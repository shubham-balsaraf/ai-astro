/**
 * Krama — Frontend logic
 *
 * Handles geocoding for birth place (using free Nominatim API)
 * and form validation.
 */

(function () {
    "use strict";

    var placeInput = document.getElementById("birth_place");
    if (!placeInput) return;

    var suggestionsBox = document.getElementById("placeSuggestions");
    var latField = document.getElementById("latitude");
    var lngField = document.getElementById("longitude");
    var submitBtn = document.getElementById("submitBtn");
    var debounceTimer = null;

    function updateSubmitState() {
        var dateOk = document.getElementById("birth_date").value;
        var timeOk = document.getElementById("birth_time").value;
        var coordsOk = latField.value && lngField.value;
        submitBtn.disabled = !(dateOk && timeOk && coordsOk);
    }

    var dateInput = document.getElementById("birth_date");
    var timeInput = document.getElementById("birth_time");
    if (dateInput) dateInput.addEventListener("change", updateSubmitState);
    if (timeInput) timeInput.addEventListener("change", updateSubmitState);

    var tzField = document.getElementById("tz_offset");
    if (tzField && !tzField.value) {
        var offset = -new Date().getTimezoneOffset();
        var sign = offset >= 0 ? "+" : "-";
        var h = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
        var m = String(Math.abs(offset) % 60).padStart(2, "0");
        tzField.value = sign + h + ":" + m;
    }

    updateSubmitState();

    placeInput.addEventListener("input", function () {
        clearTimeout(debounceTimer);
        latField.value = "";
        lngField.value = "";
        updateSubmitState();

        var query = placeInput.value.trim();
        if (query.length < 3) {
            suggestionsBox.classList.remove("active");
            return;
        }

        debounceTimer = setTimeout(function () {
            fetchPlaces(query);
        }, 350);
    });

    function fetchPlaces(query) {
        var url =
            "https://nominatim.openstreetmap.org/search?format=json&limit=5&q=" +
            encodeURIComponent(query);

        fetch(url, {
            headers: { Accept: "application/json" },
        })
            .then(function (r) {
                return r.json();
            })
            .then(function (results) {
                renderSuggestions(results);
            })
            .catch(function () {
                suggestionsBox.classList.remove("active");
            });
    }

    function renderSuggestions(results) {
        suggestionsBox.innerHTML = "";
        if (!results.length) {
            suggestionsBox.classList.remove("active");
            return;
        }

        results.forEach(function (place) {
            var div = document.createElement("div");
            div.className = "suggestion-item";
            div.textContent = place.display_name;
            div.addEventListener("click", function () {
                placeInput.value = place.display_name;
                latField.value = place.lat;
                lngField.value = place.lon;
                suggestionsBox.classList.remove("active");
                updateSubmitState();
            });
            suggestionsBox.appendChild(div);
        });

        suggestionsBox.classList.add("active");
    }

    document.addEventListener("click", function (e) {
        if (!suggestionsBox.contains(e.target) && e.target !== placeInput) {
            suggestionsBox.classList.remove("active");
        }
    });
})();
