(() => {
    const banner = document.getElementById("hq-update-banner");
    const refresh = document.getElementById("hq-update-refresh");
    const latestLabel = document.getElementById("hq-latest-version");
    const current = banner.dataset.build;
    const automatic = banner.dataset.automatic === "true";
    let latest = current;
    let interacted = false;
    let checking = false;
    let reloading = false;

    function reload() {
        if (reloading) return;
        reloading = true;
        window.location.reload();
    }

    // Treat all workspace edits and actions as potentially unsaved for this visit.
    for (const event of ["input", "change", "submit", "click"]) {
        document.addEventListener(event, (interaction) => {
            if (interaction.target !== refresh) interacted = true;
        }, true);
    }

    refresh.addEventListener("click", () => {
        if (latest === current) return;
        if (!automatic && interacted && !window.confirm(
            "Refreshing may discard unfinished work. Refresh now?"
        )) return;
        reload();
    });

    async function check() {
        if (document.visibilityState === "hidden" || checking || reloading) return;
        checking = true;
        try {
            const response = await fetch("/api/hq/version", {
                cache: "no-store",
                credentials: "omit",
                redirect: "error",
            });
            if (!response.ok) return;
            const {version} = await response.json();
            if (typeof version !== "string" || !/^[a-zA-Z0-9._-]{1,128}$/.test(version)) return;
            latest = version;
            if (latestLabel) latestLabel.textContent = latest;
            banner.hidden = latest === current;
            if (!banner.hidden && automatic && !interacted) {
                // Fall back to the button if storage is unavailable or a stale
                // shell already attempted to reload for this release.
                const key = "hq-auto-reload-version";
                if (sessionStorage.getItem(key) !== latest) {
                    sessionStorage.setItem(key, latest);
                    reload();
                }
            }
        } catch {
            // Keep the last verified version and retry while the app is visible.
        } finally {
            checking = false;
        }
    }

    window.addEventListener("pageshow", check);
    window.addEventListener("focus", check);
    window.addEventListener("online", check);
    document.addEventListener("visibilitychange", check);
    window.setInterval(check, 60000);
    check();
})();
