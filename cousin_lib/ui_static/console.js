// The console is a projection: it reads the framework's JSON views and
// renders them. It holds nothing the server could not hand it again on
// the next poll, which is the browser-side of the kill-9 rule.
"use strict";

async function refresh() {
    try {
        const health = await (await fetch("api/health")).json();
        document.getElementById("status").textContent =
            "framework at " + health.root + " - " + health.cousins +
            " cousin(s)";
        const data = await (await fetch("api/cousins")).json();
        const fleet = document.getElementById("fleet");
        fleet.innerHTML = "";
        for (const c of data.cousins) {
            const el = document.createElement("div");
            el.className = "cousin";
            const slug = document.createElement("div");
            slug.className = "slug";
            slug.textContent = c.name + " (" + c.slug + ")";
            const meta = document.createElement("div");
            meta.className = "meta";
            meta.textContent = "port " + (c.chat_port || "-") +
                " - " + c.type;
            el.append(slug, meta);
            fleet.append(el);
        }
    } catch (err) {
        document.getElementById("status").textContent =
            "cannot reach the framework: " + err;
    }
}

refresh();
setInterval(refresh, 5000);
