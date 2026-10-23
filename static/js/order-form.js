(function () {
  'use strict';

  var form = document.getElementById('order-form');
  var pickup = document.getElementById('is-pickup');
  var addrWrap = document.getElementById('address-wrap');
  var addr = document.getElementById('address-input');
  var dateInput = document.getElementById('delivery-date');
  var timeSelect = document.getElementById('delivery-time');
  var dateHint = document.getElementById('date-hint');
  if (!form || !pickup || !addrWrap || !addr || !dateInput || !timeSelect) return;

  var openingHour = Number(form.dataset.openingHour);
  var closingHour = Number(form.dataset.closingHour);
  var maxDate = form.dataset.maxDate;
  var serverTime = Date.parse(form.dataset.shopTime);
  var loadedAt = performance.now();
  var clock = new Intl.DateTimeFormat('en-CA', {
    timeZone: form.dataset.timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  });

  function togglePickup() {
    addrWrap.hidden = pickup.checked;
    addr.required = !pickup.checked;
  }

  // Текущее время магазина: не зависит от часов и часового пояса покупателя.
  function shopClock() {
    var parts = {};
    clock.formatToParts(new Date(serverTime + performance.now() - loadedAt)).forEach(function (part) {
      parts[part.type] = part.value;
    });
    var y = Number(parts.year), m = Number(parts.month), d = Number(parts.day);
    var next = new Date(Date.UTC(y, m - 1, d + 1));
    return {
      today: parts.year + '-' + parts.month + '-' + parts.day,
      tomorrow: next.toISOString().slice(0, 10),
      hour: Number(parts.hour) + Number(parts.minute) / 60 + Number(parts.second) / 3600
    };
  }

  function slotAvailableToday(opt, hour) {
    if (opt.dataset.asap) return hour >= openingHour && hour + 2 <= closingHour;
    return Number(opt.dataset.end) > hour + 1;
  }

  function updateSlots() {
    var now = shopClock();
    var todayOpen = Array.prototype.some.call(timeSelect.options, function (opt) {
      return opt.value && slotAvailableToday(opt, now.hour);
    });

    // Сегодня интервалов не осталось — переносим дату на завтра автоматически.
    var firstDate = todayOpen ? now.today : now.tomorrow;
    if (dateInput.min !== firstDate) dateInput.min = firstDate;
    if (maxDate && dateInput.max !== maxDate) dateInput.max = maxDate;
    var moved = false;
    if (!dateInput.value || dateInput.value < firstDate) {
      moved = dateInput.value !== firstDate;
      dateInput.value = firstDate;
    }
    if (dateHint && moved && !todayOpen) dateHint.hidden = false;
    if (dateHint && dateInput.value !== firstDate) dateHint.hidden = true;

    var isToday = dateInput.value === now.today;
    Array.prototype.forEach.call(timeSelect.options, function (opt) {
      if (!opt.value) return;
      var ok;
      if (opt.dataset.asap) ok = isToday && slotAvailableToday(opt, now.hour);
      else ok = !isToday || slotAvailableToday(opt, now.hour);
      opt.disabled = !ok;
    });
    var selected = timeSelect.options[timeSelect.selectedIndex];
    if (selected && selected.disabled) timeSelect.value = '';
  }

  pickup.addEventListener('change', togglePickup);
  dateInput.addEventListener('change', updateSlots);
  window.addEventListener('pageshow', updateSlots);
  setInterval(updateSlots, 30000);
  togglePickup();
  updateSlots();
})();
