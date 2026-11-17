(() => {
    const button = document.getElementById("findAddress");
    if (!button) return;
    const editor = document.getElementById("customerEditor");
    const status = document.getElementById("addressStatus");
    button.addEventListener("click", async () => {
        button.disabled = true;
        status.textContent = "Loading Google address search…";
        try {
            await new Promise((resolve, reject) => {
                const script = document.createElement("script");
                const fail = () => {
                    status.textContent = "Google address search unavailable. Enter the address manually.";
                    reject(new Error(status.textContent));
                };
                const timer = setTimeout(fail, 12000);
                window.hqAddressSearchReady = () => { clearTimeout(timer); resolve(); };
                window.gm_authFailure = fail;
                script.onerror = () => { clearTimeout(timer); fail(); };
                script.src = "https://maps.googleapis.com/maps/api/js?" + new URLSearchParams({
                    key: editor.dataset.browserKey, callback: "hqAddressSearchReady", loading: "async",
                });
                script.async = true;
                document.head.append(script);
            });
            const {PlaceAutocompleteElement} = await google.maps.importLibrary("places");
            const choices = new PlaceAutocompleteElement({
                includedRegionCodes: ["gb"], includedPrimaryTypes: ["street_address", "premise", "subpremise"],
            });
            choices.setAttribute("aria-label", "Search UK street addresses");
            document.getElementById("addressSearch").replaceChildren(choices);
            choices.addEventListener("gmp-select", async ({placePrediction}) => {
                try {
                    const place = placePrediction.toPlace();
                    await place.fetchFields({fields: ["formattedAddress", "addressComponents"]});
                    const component = place.addressComponents?.find(item => item.types.includes("postal_code"));
                    if (!place.formattedAddress || !component?.longText) {
                        throw new Error("This result has no full address and postcode.");
                    }
                    document.getElementById("customerAddress").value = place.formattedAddress;
                    document.getElementById("customerPostcode").value = component.longText;
                    status.textContent = "Address selected. Check it and tap Save customer.";
                } catch (error) {
                    status.textContent = "Could not fill that address. Try another result or type it below.";
                }
            });
            choices.addEventListener("gmp-error", () => {
                status.textContent = "Google address suggestions unavailable. You can type the address below.";
            });
            button.hidden = true;
            status.textContent = "Type a street address or postcode and choose a result.";
        } catch {
            status.textContent = "Google address search unavailable. Enter the address manually.";
        }
    });
})();
