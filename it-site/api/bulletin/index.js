/*
 * GET /api/bulletin
 *
 * Returns everything on the page that changes: notices, tips, the ticker
 * and the row of links at the top. All four live in SharePoint lists, so
 * updating the page is adding or editing a row from a phone — never a
 * commit and a build.
 *
 * The page never talks to SharePoint itself. This function holds the
 * credential, so the browser needs no Graph permission and nobody sees a
 * consent prompt.
 *
 * App registration needs Graph APPLICATION permission Sites.Selected, then
 * that one site granted to it (see README). Sites.Selected rather than
 * Sites.Read.All on purpose: this credential reads exactly one site and
 * nothing else in the tenant.
 *
 * Environment (Static Web App > Configuration):
 *   BULLETIN_TENANT_ID        4c9a50a1-c27f-4044-8025-b59b5b804d16
 *   BULLETIN_CLIENT_ID        app registration (client) id
 *   BULLETIN_CLIENT_SECRET    client secret          <- rotate on a reminder
 *   BULLETIN_SITE_ID          Graph site id (README shows how to find it)
 *   BULLETIN_LIST_ID          "Bulletin"    list id
 *   TIPS_LIST_ID              "Tips"        list id
 *   WORKSTREAMS_LIST_ID       "Workstreams" list id
 *   LINKS_LIST_ID             "Links"       list id   <- optional; leave unset
 *                                                        and the page uses the
 *                                                        tiles written into it
 */

const CACHE_SECONDS = 60;
const cache = { at: 0, body: null };

const GRAPH = 'https://graph.microsoft.com/v1.0';

/* ── field order per status ─────────────────────────────────────────────
   Anything down gets the four that matter. Everything else gets the two
   a reader actually needs. A field with nothing in it is left out. */
const SHAPE = {
  live:     [['WhatsDown', 'What’s down'], ['Why', 'Why'],
             ['BlastRadius', 'Who it hits'], ['BackBy', 'Back by']],
  planned:  [['WhatsDown', 'What’s down'], ['Why', 'Why'],
             ['BlastRadius', 'Who it hits'], ['BackBy', 'Back by']],
  notice:   [['BlastRadius', 'Who it’s for'], ['Action', 'What to do']],
  resolved: [['BlastRadius', 'Who it hit'], ['Outcome', 'Outcome']]
};

const STATUSES = ['live', 'planned', 'notice', 'resolved'];
const STATES = ['now', 'next', 'done'];

async function token() {
  const body = new URLSearchParams({
    client_id: process.env.BULLETIN_CLIENT_ID,
    client_secret: process.env.BULLETIN_CLIENT_SECRET,
    scope: 'https://graph.microsoft.com/.default',
    grant_type: 'client_credentials'
  });

  const res = await fetch(
    `https://login.microsoftonline.com/${process.env.BULLETIN_TENANT_ID}/oauth2/v2.0/token`,
    { method: 'POST', body }
  );
  if (!res.ok) throw new Error('token request failed: ' + res.status);
  return (await res.json()).access_token;
}

async function listItems(bearer, listId, top) {
  const url = `${GRAPH}/sites/${process.env.BULLETIN_SITE_ID}/lists/${listId}` +
              `/items?expand=fields&$top=${top}`;
  const res = await fetch(url, { headers: { Authorization: 'Bearer ' + bearer } });
  if (!res.ok) throw new Error(`graph ${res.status} on list ${listId}`);
  return (await res.json()).value;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* Square brackets become keys, so a tip can be typed on a phone:
   "[Ctrl] + [Shift] + [M]"  ->  three <kbd> elements. */
function keys(s) {
  return esc(s).replace(/\[([^\]]{1,14})\]/g, '<kbd>$1</kbd>');
}

function yes(v) {
  return v === undefined || v === null || v === '' || v === true ||
         String(v).toLowerCase() === 'true' || String(v).toLowerCase() === 'yes';
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 9999;
}

/* Dates read the way a person says them: Today, Yesterday, then Fri 29 Aug. */
function whenParts(iso) {
  if (!iso) return { date: '', time: '' };
  const d = new Date(iso);
  if (isNaN(d)) return { date: String(iso), time: '' };

  const midnight = x => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((midnight(new Date()) - midnight(d)) / 86400000);

  let date;
  if (days === 0) date = 'Today';
  else if (days === 1) date = 'Yesterday';
  else date = d.toLocaleDateString('en-GB',
    { weekday: 'short', day: 'numeric', month: 'short' });

  const mins = d.getHours() * 60 + d.getMinutes();
  const time = mins === 0 ? '' : d.toLocaleTimeString('en-GB',
    { hour: '2-digit', minute: '2-digit', hour12: false });

  return { date, time };
}

function toEntry(item) {
  const f = item.fields || {};
  const status = STATUSES.includes(f.Status) ? f.Status : 'notice';
  const { date, time } = whenParts(f.EventDate);

  const fields = (SHAPE[status] || [])
    .filter(([key]) => f[key] && String(f[key]).trim())
    .map(([key, label]) => {
      const value = esc(String(f[key]).trim());
      return [label, key === 'Outcome'
        ? `<span class="duration">${value}</span>` : value];
    });

  return {
    status, date, time,
    // Who posted it. The noticeboard is company-wide, so a notice with no
    // owner against it is a notice nobody can follow up.
    from: String(f.Department || 'IT').trim(),
    title: String(f.Title || '').trim(),
    body: String(f.Summary || '').trim(),
    fields,
    _sort: f.EventDate || item.createdDateTime || ''
  };
}

/* Only http(s) links get through. A list row is typed by a person, and
   javascript: in an href is the one thing that turns a noticeboard into a
   way of running code in everybody's browser. */
function safeHref(v) {
  const s = String(v || '').trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

function toLink(item) {
  const f = item.fields || {};
  return {
    name: String(f.Title || '').trim(),
    what: String(f.Description || '').trim(),
    href: safeHref(f.Url),
    _sort: num(f.SortOrder),
    _on: yes(f.Active)
  };
}

function toTip(item) {
  const f = item.fields || {};
  return {
    app: String(f.App || 'Tip').trim(),
    title: String(f.Title || '').trim(),
    body: String(f.Body || '').trim(),
    how: f.How ? keys(String(f.How).trim()) : '',
    _sort: num(f.SortOrder),
    _on: yes(f.Active)
  };
}

function toTick(item) {
  const f = item.fields || {};
  const state = STATES.includes(f.State) ? f.State : 'now';
  return {
    state,
    text: String(f.Title || '').trim(),
    _sort: num(f.SortOrder),
    _on: yes(f.Active)
  };
}

module.exports = async function (context, req) {
  try {
    if (cache.body && (Date.now() - cache.at) < CACHE_SECONDS * 1000) {
      context.res = { headers: json(), body: cache.body };
      return;
    }

    const bearer = await token();

    const [rawEntries, rawTips, rawTicks, rawLinks] = await Promise.all([
      listItems(bearer, process.env.BULLETIN_LIST_ID, 60),
      listItems(bearer, process.env.TIPS_LIST_ID, 60),
      listItems(bearer, process.env.WORKSTREAMS_LIST_ID, 60),
      process.env.LINKS_LIST_ID
        ? listItems(bearer, process.env.LINKS_LIST_ID, 30)
        : Promise.resolve([])
    ]);

    const entries = rawEntries
      .map(toEntry)
      .filter(e => e.title)
      .sort((a, b) => String(b._sort).localeCompare(String(a._sort)));

    // Anything still open floats to the top regardless of date — during an
    // outage the live item must be the first thing on the page.
    const rank = { live: 0, planned: 1, notice: 2, resolved: 2 };
    entries.sort((a, b) => rank[a.status] - rank[b.status]);
    entries.forEach(e => delete e._sort);

    const tips = rawTips
      .map(toTip)
      .filter(t => t.title && t._on)
      .sort((a, b) => a._sort - b._sort)
      .map(({ _sort, _on, ...t }) => t);

    const ticker = rawTicks
      .map(toTick)
      .filter(t => t.text && t._on)
      .sort((a, b) => a._sort - b._sort)
      .map(({ _sort, _on, ...t }) => [t.state, t.text]);

    // An empty links array is returned as empty on purpose: the page then
    // keeps the tiles written into it rather than rendering a blank row.
    const links = rawLinks
      .map(toLink)
      .filter(l => l.name && l._on)
      .sort((a, b) => a._sort - b._sort)
      .map(({ _sort, _on, ...l }) => l);

    cache.body = JSON.stringify({
      entries, tips, ticker, links, at: new Date().toISOString()
    });
    cache.at = Date.now();

    context.res = { headers: json(), body: cache.body };
  } catch (err) {
    context.log.error('bulletin: ' + err.message);

    // Serve a stale cache rather than nothing — the page's own fallback is
    // the last resort, not the first.
    if (cache.body) {
      context.res = { headers: json(), body: cache.body };
      return;
    }
    context.res = {
      status: 503,
      headers: json(),
      body: JSON.stringify({ error: 'notice list unavailable' })
    };
  }
};

function json() {
  return { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
}
