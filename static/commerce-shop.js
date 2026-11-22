(() => {
    const basket = new Map();
    const host = document.getElementById("basket");
    const output = document.getElementById("basketResult");
    let generation = 0;
    function render() {
        generation++;
        output.textContent = "";
        host.replaceChildren();
        for (const [id, item] of basket) {
            const row = document.createElement("p");
            const label = document.createElement("label");
            label.textContent = item.name;
            const quantity = document.createElement("input");
            quantity.type = "number"; quantity.min = "0"; quantity.max = "100"; quantity.step = "1";
            quantity.value = item.quantity;
            quantity.addEventListener("change", () => {
                const value = Number(quantity.value);
                if (!Number.isInteger(value) || value < 0 || value > 100) { quantity.value = item.quantity; return; }
                if (value === 0) basket.delete(id); else item.quantity = value;
                render();
            });
            label.append(quantity); row.append(label); host.append(row);
        }
    }
    for (const button of document.querySelectorAll("[data-product]")) {
        button.addEventListener("click", () => {
            const id = button.dataset.product;
            const item = basket.get(id) || { name: button.dataset.name, quantity: 0 };
            item.quantity = Math.min(100, item.quantity + 1); basket.set(id, item); render();
        });
    }
    document.getElementById("fulfilment").addEventListener("change", () => { generation++; output.textContent = ""; });
    document.getElementById("checkBasket").addEventListener("click", async () => {
        const version = generation;
        output.textContent = "Checking current prices and stock…";
        try {
            const response = await fetch("/api/shop/basket", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ lines: [...basket].map(([id, item]) => ({id, quantity: item.quantity})),
                    fulfilment: document.getElementById("fulfilment").value }),
            });
            const data = await response.json();
            if (version !== generation) return;
            if (!response.ok) throw new Error(data.error || "Unable to check basket");
            output.textContent = "Total £" + (data.total_pence / 100).toFixed(2) +
                " including delivery £" + (data.delivery_pence / 100).toFixed(2) + ". Checkout unavailable.";
        } catch (error) { if (version === generation) output.textContent = error.message; }
    });

    const requestButton = document.getElementById("requestBasket");
    if (requestButton) {
        let operation = requestButton.dataset.operation;
        const result = document.getElementById("requestResult");
        function resetOperation() {
            operation = crypto.randomUUID().replace(/-/g, "");
            result.textContent = "";
        }
        for (const add of document.querySelectorAll("[data-product]"))
            add.addEventListener("click", resetOperation);
        host.addEventListener("change", resetOperation);
        document.getElementById("fulfilment").addEventListener("change", resetOperation);
        requestButton.addEventListener("click", async () => {
            requestButton.disabled = true;
            const atVersion = generation;
            result.textContent = "Saving your request to Tommy Tints HQ…";
            try {
                const response = await fetch("/api/shop/enquiries", {
                    method: "POST",
                    headers: {"Content-Type": "application/json", "X-CSRF-Token": requestButton.dataset.csrf},
                    body: JSON.stringify({
                        operation_id: operation,
                        lines: [...basket].map(([id, item]) => ({id, quantity: item.quantity})),
                        fulfilment: document.getElementById("fulfilment").value
                    })
                });
                const data = await response.json();
                if (atVersion !== generation) return;
                if (!response.ok) throw new Error(data.error || "Could not save request");
                result.replaceChildren(document.createTextNode("Request received for owner review. Reference: " + data.id + ". "),
                    Object.assign(document.createElement("a"), {href:"/customer/shop/enquiries",textContent:"Track status"}));
            } catch (error) {
                if (atVersion === generation) result.textContent = error.message;
            } finally { requestButton.disabled = false; }
        });
    }
})();
