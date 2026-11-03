/* ============================================================================
 * consent.js — cookie consent + geo-gated leadsy.ai analytics
 *
 * ONE file, replacing eight blocks duplicated across eleven pages. That
 * duplication is why this exists: the markup, the styles, the loader and the
 * privacy-policy wording all had to agree in eleven places, and the first one
 * to drift would have been the one nobody noticed.
 *
 * Two behaviours:
 *
 *   1. Visitors where consent is legally required (EU/EEA/UK/CH) see the
 *      banner. Nothing loads until they accept.
 *
 *   2. Everyone else is tracked without a banner, under a privacy-policy
 *      notice rather than a consent gate.
 *
 * Why the split: GDPR applies by VISITOR location, not by where the site is
 * hosted, and prior consent is required there for analytics tags. Most other
 * jurisdictions — including the US, which is the majority of this audience —
 * require notice rather than opt-in. Showing a consent wall to everyone was
 * suppressing the large majority of measurable traffic to satisfy a rule that
 * applies to a minority of it.
 *
 * Country comes from Cloudflare's /cdn-cgi/trace, available on every proxied
 * domain with no Worker.
 *
 * FAILS CLOSED. An unknown country, a failed fetch, a site not behind
 * Cloudflare — all show the banner. A network error must never be allowed to
 * silently become consent.
 *
 * Usage — replace the whole consent block on each page with:
 *     <script src="/consent.js" defer></script>
 * ========================================================================== */
(function () {
  'use strict';

  var KEY = 'mag-analytics-consent';        // 'granted' | 'denied'

  // EU + EEA + UK + Switzerland.
  //
  // Deliberately wide. Including a country that did not need the banner costs
  // one banner; excluding one that did is a compliance gap — so where the
  // boundary is arguable, it goes in.
  var CONSENT_REQUIRED = [
    'AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT',
    'LV','LT','LU','MT','NL','PL','PT','RO','SK','SI','ES','SE',   // EU
    'IS','LI','NO',                                                // EEA
    'GB','CH'                                                      // UK, Switzerland
  ];

  // ── the tag ───────────────────────────────────────────────────────────────
  function loadAnalytics() {
    if (document.getElementById('vtag-ai-js')) return;   // exactly once
    var s = document.createElement('script');
    s.id = 'vtag-ai-js';
    s.async = true;
    s.src = 'https://r2.leadsy.ai/tag.js';
    s.setAttribute('data-pid', 'az6D5saf0GDIfc01');
    s.setAttribute('data-version', '062024');
    document.head.appendChild(s);

    // RB2B visitor identification - same consent rule as the tag above.
    if (!window.reb2b) {
      window.reb2b = { loaded: true };
      var r = document.createElement('script');
      r.async = true;
      r.src = 'https://ddwl4m2hdecbv.cloudfront.net/b/46DJ4HVZ8861/46DJ4HVZ8861.js.gz';
      document.head.appendChild(r);
    }
  }

  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function read()   { try { return localStorage.getItem(KEY); } catch (e) { return null; } }

  // ── banner markup, injected rather than duplicated per page ──────────────
  var CSS = [
    '.mag-consent{position:fixed;left:0;right:0;bottom:0;z-index:9999;',
    '  background:var(--bg-card,#12161f);border-top:1px solid var(--border,#2a3040);',
    '  box-shadow:0 -8px 40px -12px rgba(0,0,0,.6);',
    '  animation:magConsentUp .35s ease-out;}',
    '@keyframes magConsentUp{from{transform:translateY(100%)}to{transform:translateY(0)}}',
    '.mag-consent[hidden]{display:none}',
    '.mag-consent-inner{max-width:1100px;margin:0 auto;padding:14px 20px;',
    '  display:flex;gap:18px;align-items:center;flex-wrap:wrap}',
    '.mag-consent-text{flex:1;min-width:260px;font-size:13px;line-height:1.6;',
    '  color:var(--text-secondary,#9aa4b8)}',
    '.mag-consent-text a{color:var(--accent,#3b82f6)}',
    '.mag-consent-actions{display:flex;gap:8px}',
    '.mag-consent-btn{font-size:13px;padding:7px 16px;border-radius:6px;',
    '  cursor:pointer;border:1px solid var(--border,#2a3040);background:transparent;',
    '  color:var(--text-secondary,#9aa4b8)}',
    '.mag-consent-accept{background:var(--accent,#3b82f6);border-color:var(--accent,#3b82f6);',
    '  color:#fff}',
    '@media(max-width:640px){.mag-consent-actions{justify-content:flex-end}}'
  ].join('');

  var HTML =
    '<div class="mag-consent-inner">' +
      '<div class="mag-consent-text">' +
        'We use third-party analytics and visitor-identification tags to understand ' +
        'site traffic and improve Magertron. They load only if you accept. See our ' +
        '<a href="/privacy.html">Privacy Policy</a> for details.' +
      '</div>' +
      '<div class="mag-consent-actions">' +
        '<button type="button" class="mag-consent-btn mag-consent-decline" ' +
                'id="magConsentDecline">Decline</button>' +
        '<button type="button" class="mag-consent-btn mag-consent-accept" ' +
                'id="magConsentAccept">Accept</button>' +
      '</div>' +
    '</div>';

  function showBanner() {
    if (document.getElementById('magConsent')) return;

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var el = document.createElement('div');
    el.id = 'magConsent';
    el.className = 'mag-consent';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-live', 'polite');
    el.setAttribute('aria-label', 'Cookie consent');
    el.innerHTML = HTML;
    document.body.appendChild(el);

    document.getElementById('magConsentAccept')
      .addEventListener('click', function () {
        store('granted'); el.remove(); loadAnalytics();
      });
    document.getElementById('magConsentDecline')
      .addEventListener('click', function () {
        store('denied'); el.remove();
      });
  }

  // ── decide ────────────────────────────────────────────────────────────────
  function decide() {
    var prior = read();

    // A recorded choice wins everywhere, in both directions.
    //
    // Including for visitors who no longer need to be asked: somebody who
    // declined once should not start being tracked because they crossed a
    // border or because this policy changed. A stored 'denied' is a stated
    // preference, not a jurisdictional artefact.
    if (prior === 'granted') { loadAnalytics(); return; }
    if (prior === 'denied')  { return; }

    fetch('/cdn-cgi/trace', { cache: 'no-store' })
      .then(function (r) { return r.text(); })
      .then(function (t) {
        var m = /^loc=([A-Z]{2})/m.exec(t);
        var country = m ? m[1] : null;

        if (!country || CONSENT_REQUIRED.indexOf(country) !== -1) {
          showBanner();          // required, or unknown → ask
        } else {
          loadAnalytics();       // notice-based jurisdiction → load
        }
      })
      .catch(function () {
        // Not behind Cloudflare, offline, blocked by an extension. Ask.
        showBanner();
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', decide);
  } else {
    decide();
  }
})();
