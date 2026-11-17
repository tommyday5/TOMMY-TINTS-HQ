(() => {
    const notice = document.getElementById("hq-passkey-status");
    if (!notice) return;
    const csrf = document.querySelector('meta[name="hq-csrf"]')?.content || "";
    const supported = window.PublicKeyCredential && navigator.credentials &&
        PublicKeyCredential.parseCreationOptionsFromJSON && PublicKeyCredential.parseRequestOptionsFromJSON;

    async function send(path, value) {
        const response = await fetch("/api/hq/passkeys/" + path, {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-HQ-Passkey": "1", "X-HQ-CSRF": csrf },
            body: JSON.stringify(value),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Passkey request could not be completed");
        return result;
    }

    for (const button of document.querySelectorAll("[data-passkey]")) {
        button.addEventListener("click", async () => {
            button.disabled = true;
            notice.textContent = "";
            const action = button.dataset.passkey;
            try {
                if (action === "remove") {
                    if (!window.confirm("Remove this passkey from HQ?")) return;
                    await send("remove", { id: button.dataset.credential });
                    button.closest("li").remove();
                    notice.textContent = "Passkey removed.";
                    return;
                }
                if (!supported) throw new Error("Passkeys need a compatible browser or iPhone software version. You can sign in with Google.");
                const options = await send(action + "/options", {});
                const credential = action === "register"
                    ? await navigator.credentials.create({ publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options) })
                    : await navigator.credentials.get({ publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options) });
                if (!credential) throw new Error("No passkey was selected.");
                await send(action + "/verify", credential.toJSON());
                if (action === "login") window.location.replace("/");
                else notice.textContent = "Passkey added. Reopen Settings to manage it.";
            } catch (error) {
                notice.textContent = error.name === "NotAllowedError" ? "Passkey request cancelled or unavailable." : error.message;
            } finally {
                button.disabled = false;
            }
        });
    }
})();
