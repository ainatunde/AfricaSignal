// Progressive enhancement only: every page works without this file.
(function () {
  "use strict";

  var wrap = document.getElementById("geo-wrap");
  var button = document.getElementById("geo");
  var status = document.getElementById("geo-status");
  if (wrap && button && "geolocation" in navigator && window.fetch) {
    wrap.hidden = false;
    button.addEventListener("click", function () {
      status.textContent = "Finding your area…";
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          // The position goes in a request body, never in a URL, and is not kept by the server.
          fetch("/places/locate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude })
          })
            .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
            .then(function (data) {
              var place = data.lga || data.state;
              if (place && place.code) {
                window.location.href = "/?place=" + encodeURIComponent(place.code);
              } else {
                status.textContent = "We could not match your position to a place in Nigeria. Please search instead.";
              }
            })
            .catch(function () {
              status.textContent = "Something went wrong. Please search instead.";
            });
        },
        function () {
          status.textContent = "We could not get your position. Please search instead.";
        },
        { maximumAge: 600000, timeout: 15000 }
      );
    });
  }

  var share = document.querySelector("[data-share]");
  if (share && navigator.clipboard) {
    share.addEventListener("click", function (event) {
      event.preventDefault();
      var url = new URL(share.getAttribute("href"), window.location.href).href;
      navigator.clipboard.writeText(url).then(function () {
        share.textContent = "Link copied";
        var beacon = share.getAttribute("data-beacon");
        if (beacon && navigator.sendBeacon) { navigator.sendBeacon(beacon); }
      });
    });
  }
})();
