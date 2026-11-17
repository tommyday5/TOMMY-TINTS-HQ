(() => {
    const byId = id => document.getElementById(id);
    const root = byId("routePlanner");
    const message = byId("routeMessage");
    const customers = new Map();
    const markers = new Map();
    const selected = [];
    let map;
    let lines = [];
    let routeGeometry = null;
    let routing = false;

    function button(label, action) {
        const node = document.createElement("button");
        node.type = "button";
        node.textContent = label;
        node.addEventListener("click", action);
        return node;
    }

    function text(tag, value) {
        const node = document.createElement(tag);
        node.textContent = value;
        return node;
    }

    function summary(customer) {
        return [customer.frequency, customer.due, customer.status].filter(Boolean).join(" · ");
    }

    function clearRoute() {
        lines.forEach(line => line.setMap(null));
        lines = [];
        routeGeometry = null;
        byId("routeTotals").replaceChildren();
    }

    function select(id) {
        if (routing) return;
        const index = selected.indexOf(id);
        if (index >= 0) selected.splice(index, 1);
        else if (!customers.get(id).address) {
            message.textContent = "Add a full service address and postcode in the maintenance sheet.";
            return;
        } else if (selected.length === 10) {
            message.textContent = "Select up to 10 route stops at a time.";
            return;
        } else selected.push(id);
        clearRoute();
        render();
    }

    function reorder(index, offset) {
        if (routing) return;
        const other = index + offset;
        [selected[index], selected[other]] = [selected[other], selected[index]];
        clearRoute();
        render();
    }

    function colour(customer, index) {
        if (index >= 0 || customer.status === "due soon") return "#ffcc38";
        if (customer.status === "overdue") return "#ff4657";
        if (customer.status === "completed") return "#86dfa4";
        if (customer.status === "scheduled") return "#9cc7ff";
        return "#dddde5";
    }

    function render() {
        const cards = byId("customerList");
        const filter = byId("filter").value;
        cards.replaceChildren();
        for (const [id, data] of customers) {
            const index = selected.indexOf(id);
            const marker = markers.get(id);
            if (marker) {
                marker.setVisible(!filter || data.status === filter || index >= 0);
                marker.setIcon({path: google.maps.SymbolPath.CIRCLE, scale: index >= 0 ? 12 : 9,
                    fillColor: colour(data, index), fillOpacity: 1, strokeColor: "#17191e", strokeWeight: 2});
                marker.setLabel(index < 0 ? null : {text: String(index + 1), color: "#131313"});
            }
            if (filter && data.status !== filter) continue;
            const card = button(data.name, () => select(id));
            card.setAttribute("aria-pressed", String(index >= 0));
            const details = text("span", summary(data) + (data.address ? "" : " · Full address needed"));
            details.dataset.status = data.status;
            card.append(details);
            const group = document.createElement("div");
            const edit = text("a", "Edit customer");
            edit.href = "/hq/maintenance/customer/edit?" + new URLSearchParams({id});
            edit.setAttribute("aria-label", "Edit " + data.name);
            group.append(card, edit);
            cards.append(group);
        }
        if (!cards.children.length) cards.append(text("p", "No customers match this view."));
        const list = byId("stopList");
        list.replaceChildren();
        selected.forEach((id, index) => {
            const data = customers.get(id);
            const row = text("li", data.name);
            const details = text("span", summary(data));
            details.className = "stopInfo";
            row.append(details);
            for (const [label, delta] of [["Move up", -1], ["Move down", 1]]) {
                const move = button(label, () => reorder(index, delta));
                move.setAttribute("aria-label", label + " for " + data.name);
                move.disabled = routing || index + delta < 0 || index + delta >= selected.length;
                row.append(move);
            }
            row.append(button("Remove", () => select(id)));
            const destination = encodeURIComponent(data.address);
            for (const [label, href] of [
                ["Apple Maps", "https://maps.apple.com/?dirflg=d&daddr=" + destination],
                ["Google Maps", "https://www.google.com/maps/dir/?api=1&destination=" + destination],
            ]) {
                const link = text("a", label);
                link.href = href;
                link.rel = "noreferrer";
                link.target = "_blank";
                row.append(link);
            }
            list.append(row);
        });
        if (!selected.length) list.append(text("li", "Select customer cards or map pins."));
        byId("optimise").disabled = byId("calculate").disabled =
            routing || !selected.length || root.dataset.routing !== "yes";
    }

    async function requestRoute(payload) {
        const csrf = document.querySelector('meta[name="hq-csrf"]').content;
        const response = await fetch("/api/hq/maintenance/visual-route", {
            credentials: "same-origin",
            ...(payload ? {method: "POST", headers: {"Content-Type": "application/json", "X-HQ-CSRF": csrf},
                body: JSON.stringify(payload)} : {}),
        });
        if (response.redirected) throw new Error("Sign back into HQ to load maintenance records.");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to load route");
        return data;
    }

    function draw(polyline) {
        if (!map) return;
        const path = google.maps.geometry.encoding.decodePath(polyline);
        for (const [width, opacity] of [[12, 0.22], [4, 0.95]]) {
            lines.push(new google.maps.Polyline({map, path, strokeColor: "#f81937", strokeWeight: width,
                strokeOpacity: opacity, clickable: false}));
        }
        const bounds = new google.maps.LatLngBounds();
        path.forEach(point => bounds.extend(point));
        map.fitBounds(bounds, 44);
    }

    async function calculate(optimise) {
        if (routing) return;
        routing = true;
        clearRoute();
        render();
        message.textContent = "Checking the route with Google…";
        try {
            const result = await requestRoute({ids: selected.slice(), optimise});
            selected.splice(0, selected.length, ...result.stop_ids);
            routeGeometry = result.polyline;
            draw(routeGeometry);
            const totals = byId("routeTotals");
            for (const line of [
                result.miles + " miles · " + result.minutes + " min estimated drive",
                "Estimated revenue " + (result.revenue === null ? "unavailable" : "£" + result.revenue),
                "Estimated driving cost £" + result.mileage + " at £" + result.rate + "/mile",
                "After mileage " + (result.after_mileage === null ? "unavailable" : "£" + result.after_mileage),
            ]) totals.append(text("p", line));
            message.textContent = "Google driving estimate, workshop round trip. Traffic and service time are excluded.";
        } catch (error) {
            message.textContent = error.message;
        } finally {
            routing = false;
            render();
        }
    }

    async function loadMap() {
        const control = byId("loadMap");
        const status = byId("mapMessage");
        control.disabled = true;
        status.textContent = "Loading Google Maps…";
        try {
            await new Promise((resolve, reject) => {
                const script = document.createElement("script");
                window.hqRouteMapReady = resolve;
                window.gm_authFailure = () => {
                    status.textContent = "Google Maps rejected the browser key.";
                    reject(new Error(status.textContent));
                };
                script.src = "https://maps.googleapis.com/maps/api/js?" + new URLSearchParams({
                    key: root.dataset.browserKey, libraries: "geometry", callback: "hqRouteMapReady", loading: "async",
                });
                script.async = true;
                script.onerror = () => reject(new Error("Google Maps could not be loaded."));
                document.head.append(script);
            });
            const surface = byId("map");
            surface.hidden = false;
            map = new google.maps.Map(surface, {center: {lat: 51.55, lng: 0.14}, zoom: 10,
                mapTypeControl: false, streetViewControl: false,
                styles: [{elementType: "geometry", stylers: [{color: "#292a30"}]},
                    {elementType: "labels.text.fill", stylers: [{color: "#dddddf"}]},
                    {elementType: "labels.text.stroke", stylers: [{color: "#212126"}]},
                    {featureType: "water", elementType: "geometry", stylers: [{color: "#141922"}]}]});
            const geocoder = new google.maps.Geocoder();
            const bounds = new google.maps.LatLngBounds();
            const items = [...customers.values()].filter(item => item.address).slice(0, 40);
            for (const customer of items) {
                try {
                    const {results} = await geocoder.geocode({address: customer.address, region: "gb",
                        componentRestrictions: {country: "GB"}});
                    const result = results[0];
                    if (!result || result.partial_match || result.geometry.location_type !== "ROOFTOP") continue;
                    const marker = new google.maps.Marker({map, position: result.geometry.location, title: customer.name});
                    marker.addListener("click", () => {
                        byId("markerDetail").replaceChildren(text("p", customer.name + " · " + summary(customer)));
                        select(customer.id);
                    });
                    markers.set(customer.id, marker);
                    bounds.extend(result.geometry.location);
                    render();
                } catch {
                    // Keep unverified coordinates off the map; cards remain usable.
                }
            }
            if (routeGeometry) draw(routeGeometry);
            else if (markers.size) map.fitBounds(bounds, 42);
            status.textContent = markers.size + " verified address pins of " + customers.size +
                " customers. Remaining stops are available as cards; plotting is limited to 40 addresses per load.";
            control.hidden = true;
        } catch (error) {
            status.textContent = error.message;
            control.disabled = false;
        }
    }

    byId("filter").addEventListener("change", render);
    byId("optimise").addEventListener("click", () => calculate(true));
    byId("calculate").addEventListener("click", () => calculate(false));
    const load = byId("loadMap");
    if (load) {
        load.disabled = true;
        load.addEventListener("click", loadMap);
    }
    requestRoute().then(({customers: items}) => {
        items.sort((a, b) => (a.due || "9999").localeCompare(b.due || "9999"));
        for (const item of items) customers.set(item.id, item);
        message.textContent = items.length + " active maintenance customers.";
        if (load) load.disabled = false;
        render();
    }).catch(error => {
        message.replaceChildren(text("span", error.message + " "));
        const link = text("a", "Connect Google Sheets");
        link.href = "/auth/sheets/connect";
        message.append(link);
    });
})();
