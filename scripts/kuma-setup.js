// Configures Uptime Kuma (admin, monitors, status page, Telegram). Safe to re-run.
// ssh ops "cd /opt/ops && docker compose exec -T uptime-kuma sh -c 'cat > /tmp/kuma-setup.js'" < scripts/kuma-setup.js
// then: docker compose exec -T -e KUMA_USER=nabil -e KUMA_PW=... [-e TG_TOKEN=... -e TG_CHAT=...] [-e BACKUP_PUSH_TOKEN=...] [-e SCHOOL_BACKUP_PUSH_TOKEN=...] uptime-kuma node /tmp/kuma-setup.js
const { io } = require("/app/node_modules/socket.io-client");

const { KUMA_USER, KUMA_PW, TG_TOKEN, TG_CHAT, BACKUP_PUSH_TOKEN, SCHOOL_BACKUP_PUSH_TOKEN } = process.env;
// Push monitors (dead man's switches), each created only when its token is set.
const PUSH_MONITORS = [
    ["Nightly backup", BACKUP_PUSH_TOKEN, "Pinged by ops-backup after each successful run"],
    ["School backup", SCHOOL_BACKUP_PUSH_TOKEN, "Pinged by dlz-offsite on the school server after each upload"],
];
const MONITORS = [
    ["Language school (deutscheslernzentrum.de)", "https://deutscheslernzentrum.de"],
    ["CareTrack", "https://caretrack-25m.pages.dev"],
    ["Portfolio", "https://nabil-sehli.github.io/portfolio/"],
    ["n8n automations", "https://n8n.nabil-ops.duckdns.org/healthz"],
];
// Watchdogs for the alerting stack itself, reached on the internal docker
// network and kept off the public status page. Prometheus cannot alert on its
// own absence and Alertmanager is the path every alert takes, so on
// 2026-09-18 both sat dead for 35 hours without a word. Kuma is a separate
// process with its own Telegram notification, and it is what watches these.
const INTERNAL_MONITORS = [
    ["Prometheus (internal)", "http://prometheus:9090/-/healthy",
        "Nothing else notices if Prometheus stops: every alert rule is evaluated inside it"],
    ["Alertmanager (internal)", "http://alertmanager:9093/-/healthy",
        "The path every Prometheus alert takes out of the box"],
];
const SLUG = "ops";
const TITLE = "Nabil Sehli - Service Status";

const socket = io("http://localhost:3001", { transports: ["websocket"] });
const call = (event, ...args) => new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(event + " timed out")), 30000);
    socket.emit(event, ...args, (res) => { clearTimeout(t); resolve(res); });
});
const must = (res, what) => { if (res && res.ok === false) throw new Error(what + ": " + res.msg); return res; };
// Monitors added over the socket don't inherit the default notification the
// way ones created in the UI do; they have to name it.
const findTelegram = async () => {
    for (let i = 0; notificationList === null && i < 50; i++) await new Promise((r) => setTimeout(r, 200));
    return Object.values(notificationList || []).find((n) => n.name === "Telegram");
};

let monitorList = null;
socket.on("monitorList", (list) => { monitorList = list; });
let notificationList = null;
socket.on("notificationList", (list) => { notificationList = list; });
// The server awaits sendInfo() before registering its handlers, so anything
// emitted before the first "info" event is silently dropped.
const ready = new Promise((r) => socket.once("info", r));

async function main() {
    await ready;
    await new Promise((r) => setTimeout(r, 500));

    if (await call("needSetup")) {
        must(await call("setup", KUMA_USER, KUMA_PW), "setup");
        console.log("admin account created");
    }
    must(await call("login", { username: KUMA_USER, password: KUMA_PW, token: "" }), "login");
    for (let i = 0; monitorList === null && i < 50; i++) await new Promise((r) => setTimeout(r, 200));
    const byName = Object.fromEntries(Object.values(monitorList || {}).map((m) => [m.name, m.id]));

    const ids = [];
    for (const [name, url] of MONITORS) {
        if (byName[name]) { ids.push(byName[name]); console.log("exists:", name); continue; }
        const res = must(await call("add", {
            type: "http", name, url, method: "GET",
            interval: 60, retryInterval: 60, resendInterval: 0, maxretries: 2, timeout: 48,
            expiryNotification: true, ignoreTls: false, upsideDown: false, maxredirects: 10,
            accepted_statuscodes: ["200-299"], notificationIDList: {},
            kafkaProducerBrokers: [], kafkaProducerSaslOptions: { mechanism: "None" },
            conditions: [], rabbitmqNodes: [], httpBodyEncoding: "json", description: "",
        }), "add " + name);
        ids.push(res.monitorID);
        console.log("added:", name, res.monitorID);
    }

    const created = await call("addStatusPage", TITLE, SLUG);
    console.log("status page:", created.ok ? "created" : /UNIQUE/.test(created.msg) ? "exists" : created.msg);
    must(await call("saveStatusPage", SLUG, {
        slug: SLUG, title: TITLE,
        description: "Live status of the services I build and run, checked every minute from my own ops server.",
        autoRefreshInterval: 300, theme: "auto", showTags: false, footerText: "", customCSS: "",
        showPoweredBy: false, rssTitle: null, showOnlyLastHeartbeat: false, showCertificateExpiry: true,
        analyticsId: null, analyticsScriptUrl: null, analyticsType: null, domainNameList: [],
    }, "/icon.svg", [{ name: "Services", monitorList: ids.map((id) => ({ id })) }]), "saveStatusPage");
    console.log("status page saved with", ids.length, "monitors");

    if (TG_TOKEN && TG_CHAT) {
        const telegram = {
            name: "Telegram", type: "telegram", isDefault: true, applyExisting: true,
            telegramBotToken: TG_TOKEN, telegramChatID: TG_CHAT,
            telegramSendSilently: false, telegramProtectContent: false,
        };
        const existing = Object.values(notificationList || []).find((n) => n.name === "Telegram");
        // Default for new monitors and applied to every existing one.
        must(await call("addNotification", telegram, existing ? existing.id : null), "addNotification");
        console.log("telegram notification", existing ? "updated" : "added", "for all monitors");
        must(await call("testNotification", telegram), "testNotification");
        console.log("test alert sent");
    }

    // Dead man's switches: go DOWN (and alert) when no ping arrives for 26 h.
    // Kept off the public status page.
    for (const [name, token, description] of PUSH_MONITORS) {
        if (!token) continue;
        if (byName[name]) { console.log("exists:", name); continue; }
        const tg = await findTelegram();
        const res = must(await call("add", {
            type: "push", name, pushToken: token,
            interval: 26 * 3600, retryInterval: 3600, resendInterval: 0, maxretries: 0,
            upsideDown: false, accepted_statuscodes: ["200-299"],
            notificationIDList: tg ? { [tg.id]: true } : {},
            kafkaProducerBrokers: [], kafkaProducerSaslOptions: { mechanism: "None" },
            conditions: [], rabbitmqNodes: [], description,
        }), "add " + name);
        console.log("added:", name, res.monitorID, tg ? "with Telegram" : "WITHOUT Telegram");
    }

    for (const [name, url, description] of INTERNAL_MONITORS) {
        if (byName[name]) { console.log("exists:", name); continue; }
        const tg = await findTelegram();
        const res = must(await call("add", {
            type: "http", name, url, method: "GET",
            interval: 60, retryInterval: 60, resendInterval: 0, maxretries: 2, timeout: 20,
            // An internal name over plain HTTP: no certificate to expire.
            expiryNotification: false, ignoreTls: false, upsideDown: false, maxredirects: 0,
            accepted_statuscodes: ["200-299"],
            notificationIDList: tg ? { [tg.id]: true } : {},
            kafkaProducerBrokers: [], kafkaProducerSaslOptions: { mechanism: "None" },
            conditions: [], rabbitmqNodes: [], httpBodyEncoding: "json", description,
        }), "add " + name);
        console.log("added:", name, res.monitorID, tg ? "with Telegram" : "WITHOUT Telegram");
    }
}

main().then(() => process.exit(0), (e) => { console.error("FAILED:", e.message); process.exit(1); });
