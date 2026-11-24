'use strict';

function calendarDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const millis = Date.parse(value + 'T00:00:00Z');
  if (!Number.isFinite(millis) || new Date(millis).toISOString().slice(0, 10) !== value) return NaN;
  return millis / 86400000;
}

function countTripDays(tickets, evening) {
  const times = /^([01]\d|2[0-3]):[0-5]\d$/;
  const legs = tickets.map(ticket => ({...ticket, day: calendarDay(ticket.date)}));
  if (legs.some(leg => !Number.isFinite(leg.day))) return {message: '请填写两张车票的乘车日期。'};
  legs.sort((a, b) => a.day - b.day || a.time.localeCompare(b.time));
  const [outbound, returning] = legs;
  const span = returning.day - outbound.day;
  if (!span) return {days: 1, message: '同一天往返，计 1 天。'};
  if (!times.test(outbound.time) || !times.test(evening)) {
    return {message: '跨日行程需要填写去程的发车时间和晚间起算时间。'};
  }
  const eveningDeparture = outbound.time >= evening;
  return {
    days: span + 1 - Number(eveningDeparture),
    message: eveningDeparture
      ? '去程在晚间发车，出发日不计入。'
      : '去程在晚间之前发车，出发日计入。'
  };
}

function renderTripTicket(ticket, index) {
  const travel = ticket.rail_travel || {};
  const route = travel.origin && travel.destination ? travel.origin + ' → ' + travel.destination : '';
  const descriptor = [travel.train, route, ticket.invoice_no ? '票号尾号 ' + ticket.invoice_no.slice(-6) : ''].filter(Boolean);
  const original = '/api/file/' + encodeURIComponent(ticket.id);
  return `<fieldset class="trip-ticket">
    <legend>车票 ${index + 1}</legend>
    <div class="trip-ticket-source"><span>${esc(descriptor.join(' · ') || ticket.filename)}</span>
      <a class="text-button" href="${original}" target="_blank" rel="noopener">查看原票</a></div>
    <div class="form-grid">
      <label>乘车日期<input type="date" data-trip-date value="${esc(travel.date || '')}"></label>
      <label>发车时间<input type="time" data-trip-time value="${esc(travel.time || '')}"></label>
    </div>
  </fieldset>`;
}

function refreshTripDays() {
  const tickets = $$('#trip-tickets fieldset').map(field => ({
    date: field.querySelector('[data-trip-date]').value,
    time: field.querySelector('[data-trip-time]').value
  }));
  const result = countTripDays(tickets, $('#trip-evening').value);
  $('#trip-days-total').textContent = result.days == null ? '待补充' : result.days + ' 天';
  $('#trip-days-reason').textContent = result.message;
}

function openTripDays() {
  const tickets = state.invoices.filter(row => selected.has(row.id));
  if (!canCalculateTripDays(tickets)) return;
  const [first, second] = tickets.map(ticket => ticket.rail_travel || {});
  const sameDirection = first.origin && first.destination
    && first.origin === second.origin && first.destination === second.destination;
  $('#trip-direction-note').classList.toggle('hidden', !sameDirection);
  $('#trip-evening').value = '18:00';
  $('#trip-tickets').innerHTML = tickets.map(renderTripTicket).join('');
  refreshTripDays();
  $('#trip-days-dialog').showModal();
}

$('#trip-days-button').addEventListener('click', openTripDays);
$('#trip-day-fields').addEventListener('input', refreshTripDays);
