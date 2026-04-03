// Smart interval: 30s during ET market hours, 5min otherwise.
window.dash_clientside = window.dash_clientside || {};
window.dash_clientside.bread = {
    smartInterval: function(_nIntervals, marketMs, offMs) {
        var now = new Date();
        // Convert to ET (handles DST automatically)
        var et = new Date(now.toLocaleString("en-US", {timeZone: "America/New_York"}));
        var day = et.getDay();  // 0=Sun, 1=Mon, ..., 6=Sat
        var hour = et.getHours();
        var minute = et.getMinutes();
        var timeDecimal = hour + minute / 60.0;
        // Market hours: Mon-Fri, 9:30 to 16:00 ET
        var isMarketHours = (day >= 1 && day <= 5 && timeDecimal >= 9.5 && timeDecimal < 16);
        return isMarketHours ? marketMs : offMs;
    }
};
