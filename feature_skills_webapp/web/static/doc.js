(function () {
  'use strict';

  // ---- Config injected by the template ----
  // window.__commentable: boolean
  // window.__commentPostUrl: string (present when commentable)
  // window.__prefillComments: [{id, excerpt, text}] (may be empty)

  // ---- State ----
  var comments = [];
  var nextLocalId = 1;
  var pendingSelection = null;

  // ---- DOM refs ----
  var layout = document.getElementById('layout');
  var railContainer = document.getElementById('comments-rail');
  var railList = document.getElementById('rail-list');
  var railCount = document.getElementById('rail-count');
  var submitBtn = document.getElementById('comment-submit-btn');
  var trigger = document.getElementById('comment-trigger');
  var popover = document.getElementById('comment-popover');
  var popoverExcerpt = document.getElementById('popover-excerpt');
  var popoverTextarea = document.getElementById('popover-textarea');

  // ---- TOC building ----
  function buildToc() {
    var tocList = document.getElementById('toc-list');
    if (!tocList) return;
    var docMain = document.getElementById('doc-main');
    if (!docMain) return;
    var sections = docMain.querySelectorAll('section[id]');
    if (!sections.length) {
      var navEl = document.querySelector('nav.toc');
      if (navEl) navEl.style.display = 'none';
      return;
    }
    sections.forEach(function (sec) {
      var id = sec.id;
      var h2 = sec.querySelector('h2');
      var label = h2 ? h2.textContent.trim() : id;
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.href = '#' + id;
      a.textContent = label;
      a.dataset.tocId = id;
      li.appendChild(a);
      tocList.appendChild(li);
    });
    setupScrollSpy(sections);
  }

  function setupScrollSpy(sections) {
    var tocLinks = document.querySelectorAll('nav.toc a[data-toc-id]');
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var id = entry.target.id;
          tocLinks.forEach(function (a) {
            a.classList.toggle('active', a.dataset.tocId === id);
          });
        }
      });
    }, { rootMargin: '-20% 0px -70% 0px' });
    sections.forEach(function (s) { obs.observe(s); });
  }

  // ---- Prefill ----
  function loadPrefill() {
    var prefill = window.__prefillComments || [];
    prefill.forEach(function (c) {
      comments.push({ localId: nextLocalId++, serverId: c.id, excerpt: c.excerpt || '', text: c.text, prefilled: true });
    });
    renderRail();
  }

  // ---- Comment rail ----
  function escape(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderRail() {
    if (!railList) return;
    railList.innerHTML = '';
    comments.forEach(function (c) {
      var div = document.createElement('div');
      div.className = 'rail-comment';
      div.innerHTML =
        '<button class="delete-btn" data-lid="' + c.localId + '" aria-label="Delete comment">×</button>' +
        (c.excerpt ? '<div class="excerpt">“' + escape(c.excerpt.slice(0, 120)) + (c.excerpt.length > 120 ? '…' : '') + '”</div>' : '') +
        '<div class="text">' + escape(c.text) + '</div>';
      railList.appendChild(div);
    });
    railList.querySelectorAll('.delete-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var lid = parseInt(btn.dataset.lid, 10);
        var idx = comments.findIndex(function (c) { return c.localId === lid; });
        if (idx >= 0) comments.splice(idx, 1);
        renderRail();
      });
    });
    var count = comments.length;
    if (railCount) railCount.textContent = count;
    if (layout) layout.classList.toggle('has-comments', count > 0);
    if (submitBtn) submitBtn.disabled = count === 0;
  }

  // ---- Click-to-comment ----
  function initClickToComment() {
    if (!window.__commentable) return;
    if (!trigger || !popover) return;

    document.addEventListener('mouseup', function (e) {
      if (e.target.closest && (e.target.closest('#comment-popover') || e.target.closest('#comment-trigger'))) return;
      var sel = window.getSelection();
      var text = sel ? sel.toString().trim() : '';
      if (!text || text.length > 500) {
        trigger.classList.remove('visible');
        return;
      }
      var range = sel.getRangeAt(0);
      var rect = range.getBoundingClientRect();
      trigger.style.top = (window.scrollY + rect.top - 40) + 'px';
      trigger.style.left = (window.scrollX + rect.left + rect.width / 2 - 50) + 'px';
      trigger.classList.add('visible');
      pendingSelection = { text: text, rect: rect };
    });

    trigger.addEventListener('mousedown', function (e) { e.preventDefault(); });

    trigger.addEventListener('click', function () {
      if (!pendingSelection) return;
      popoverExcerpt.textContent = '“' + pendingSelection.text.slice(0, 200) + (pendingSelection.text.length > 200 ? '…' : '') + '”';
      popoverTextarea.value = '';
      var rect = pendingSelection.rect;
      popover.style.top = (window.scrollY + rect.top + rect.height + 8) + 'px';
      popover.style.left = (window.scrollX + Math.max(20, rect.left)) + 'px';
      popover.classList.add('visible');
      trigger.classList.remove('visible');
      setTimeout(function () { popoverTextarea.focus(); }, 0);
    });

    document.getElementById('popover-cancel').addEventListener('click', function () {
      popover.classList.remove('visible');
      pendingSelection = null;
    });

    document.getElementById('popover-save').addEventListener('click', function () {
      var text = popoverTextarea.value.trim();
      if (text && pendingSelection) {
        comments.push({ localId: nextLocalId++, excerpt: pendingSelection.text, text: text, prefilled: false });
        renderRail();
      }
      popover.classList.remove('visible');
      pendingSelection = null;
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        popover.classList.remove('visible');
        trigger.classList.remove('visible');
        pendingSelection = null;
      }
    });
  }

  // ---- Submit handler ----
  function initSubmit() {
    if (!submitBtn || !window.__commentPostUrl) return;
    submitBtn.addEventListener('click', async function () {
      var payload = comments.map(function (c) {
        return { excerpt: c.excerpt || null, text: c.text };
      });
      submitBtn.disabled = true;
      submitBtn.textContent = 'Submitting…';
      try {
        var resp = await fetch(window.__commentPostUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ comments: payload }),
        });
        if (resp.ok) {
          submitBtn.textContent = 'Submitted ✓';
          submitBtn.classList.add('btn-success');
        } else {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit failed — try again';
        }
      } catch (e) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Submit failed — try again';
      }
    });
  }

  // ---- Synthesis submit ----
  function initSynthesisSubmit() {
    if (!window.__synthesisMode) return;
    var btn = document.getElementById('synthesis-submit-btn');
    if (!btn || !window.__synthesisPostUrl) return;

    // Routine flag toggle
    document.querySelectorAll('.flag-check').forEach(function (cb) {
      cb.addEventListener('change', function () {
        var detail = cb.closest('.syn-routine-item').querySelector('.flag-detail');
        if (detail) detail.hidden = !cb.checked;
      });
    });

    btn.addEventListener('click', async function () {
      var responses = {};
      document.querySelectorAll('.response-ta').forEach(function (ta) {
        responses[ta.dataset.item] = ta.value.trim();
      });
      var routine_flags = {};
      document.querySelectorAll('.flag-check:checked').forEach(function (cb) {
        var ta = cb.closest('.syn-routine-item').querySelector('.flag-ta');
        if (ta) routine_flags[cb.dataset.item] = ta.value.trim();
      });
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      try {
        var resp = await fetch(window.__synthesisPostUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ responses: responses, routine_flags: routine_flags }),
        });
        if (resp.ok) {
          btn.textContent = 'Submitted ✓';
          btn.classList.add('btn-success');
        } else {
          btn.disabled = false;
          btn.textContent = 'Submit failed — try again';
        }
      } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Submit failed — try again';
      }
    });
  }

  // ---- Init ----
  document.addEventListener('DOMContentLoaded', function () {
    buildToc();
    loadPrefill();
    initClickToComment();
    initSubmit();
    initSynthesisSubmit();
  });
})();
