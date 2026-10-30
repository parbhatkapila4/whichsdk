(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var canAnimate = !reduce && 'animate' in Element.prototype;
  var EASE = 'cubic-bezier(0.2, 0.7, 0.2, 1)';
  var COUNT_MS = 1300;

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

  var firstScreen = [];
  function onFirstScreen(el) {
    if (firstScreen.indexOf(el) !== -1) { return true; }
    var rect = el.getBoundingClientRect();
    var inView = rect.top < window.innerHeight * 0.98 && rect.bottom > 0;
    if (inView) { firstScreen.push(el); }
    return inView;
  }

  function whenVisible(els, fn, options) {
    if (!('IntersectionObserver' in window)) { return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { io.unobserve(entry.target); fn(entry.target); }
      });
    }, options || { rootMargin: '0px 0px 6% 0px', threshold: 0.01 });
    els.forEach(function (el) { io.observe(el); });
  }

  function reveals() {
    if (!canAnimate) { return; }
    $$('[data-reveal]').forEach(function (el) {
      if (onFirstScreen(el)) { return; }
      whenVisible([el], function (target) {
        target.animate(
          [{ opacity: 0, transform: 'translateY(26px)' }, { opacity: 1, transform: 'none' }],
          { duration: 850, easing: EASE, fill: 'backwards' }
        );
      });
    });
    $$('[data-stagger]').forEach(function (group) {
      if (onFirstScreen(group)) { return; }
      whenVisible([group], function (target) {
        var step = Number(target.getAttribute('data-stagger'));
        Array.prototype.slice.call(target.children).forEach(function (child, i) {
          child.animate(
            [{ opacity: 0, transform: 'translateY(30px)' }, { opacity: 1, transform: 'none' }],
            { duration: 850, delay: i * step, easing: EASE, fill: 'backwards' }
          );
        });
      });
    });
  }

  function countUps() {
    if (reduce) { return; }
    $$('[data-count]').forEach(function (el) {
      var end = parseFloat(el.getAttribute('data-count'));
      var finalText = el.textContent;
      if (onFirstScreen(el)) { return; }
      whenVisible([el], function () {
        var start = performance.now();
        (function frame(now) {
          var p = clamp((now - start) / COUNT_MS, 0, 1);
          el.textContent = Math.round(end * (1 - Math.pow(1 - p, 3)));
          if (p < 1) { requestAnimationFrame(frame); } else { el.textContent = finalText; }
        })(start);
      });
    });
  }

  function growChartBars() {
    if (!canAnimate) { return; }
    $$('[data-chart]').forEach(function (chart) {
      var from = onFirstScreen(chart) ? 0.55 : 0;
      whenVisible([chart], function () {
        $$('.row', chart).forEach(function (row, i) {
          var bar = $('.bar', row);
          var range = $('.range', row);
          if (bar) {
            bar.animate([{ transform: 'scaleX(' + from + ')' }, { transform: 'scaleX(1)' }],
              { duration: 1000, delay: from ? i * 40 : 150 + i * 80, easing: EASE, fill: 'backwards' });
          }
          if (range) {
            range.animate([{ opacity: from ? 0.35 : 0, transform: 'scaleX(' + (from ? 0.8 : 0.4) + ')' }, { opacity: 1, transform: 'none' }],
              { duration: 650, delay: from ? 300 + i * 40 : 700 + i * 80, easing: EASE, fill: 'backwards' });
          }
        });
      }, { rootMargin: '0px', threshold: 0.2 });
    });
  }

  function growHeroBars() {
    if (!canAnimate) { return; }
    $$('.ubar i').forEach(function (bar, i) {
      bar.animate([{ transform: 'scaleX(0.45)' }, { transform: 'scaleX(1)' }], { duration: 1100, delay: 200 + i * 120, easing: EASE, fill: 'backwards' });
    });
  }

  function selectText(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function copyButtons() {
    $$('[data-copy]').forEach(function (button) {
      var label = button.textContent;
      button.addEventListener('click', function () {
        var target = document.getElementById(button.getAttribute('data-copy'));
        var text = target.innerText.replace(/^\$\s*/gm, '').trim();
        function report(ok) {
          button.textContent = ok ? 'Copied' : 'Press Ctrl+C';
          setTimeout(function () { button.textContent = label; }, 1800);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { report(true); }, function () { selectText(target); report(false); });
        } else {
          selectText(target);
          report(false);
        }
      });
    });
  }

  function menu() {
    var button = $('[data-menu-button]');
    var panel = $('[data-menu]');
    function set(open) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      panel.hidden = !open;
    }
    button.addEventListener('click', function () { set(button.getAttribute('aria-expanded') !== 'true'); });
    $$('a', panel).forEach(function (a) { a.addEventListener('click', function () { set(false); }); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { set(false); } });
  }

  function rulerProgress() {
    var els = $$('[data-progress]');
    var ticking = false;
    function update() {
      ticking = false;
      els.forEach(function (el) {
        var rect = el.getBoundingClientRect();
        var p = clamp((window.innerHeight * 0.82 - rect.top) / Math.max(rect.height, 1), 0, 1);
        el.style.setProperty('--p', p.toFixed(3));
      });
    }
    function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    update();
  }

  function dimOtherLines() {
    if (!('IntersectionObserver' in window)) { return; }
    $$('[data-focus-group]').forEach(function (group) {
      var items = $$('[data-focus]', group);
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) { group.setAttribute('data-active', items.indexOf(entry.target)); }
        });
      }, { rootMargin: '-42% 0px -42% 0px', threshold: 0 });
      items.forEach(function (item) { io.observe(item); });
    });
  }

  function carousel() {
    var FIRST_DWELL_MS = 5200;
    var DWELL_MS = 4200;
    var RESUME_MS = 2500;
    var ROW_MS = 520;
    var START_MS = 150;
    var HIT_GAP_MS = 1200;

    var stage = $('[data-stage]');
    var slides = $$('.sess', stage);
    var bars = $('.ucards', stage);
    var cards = {};
    $$('.ucard[data-key]', stage).forEach(function (card) { cards[card.getAttribute('data-key')] = card; });
    var dots = $$('[data-dot]', stage);
    var play = $('[data-play]', stage);
    var index = 0;
    var auto = canAnimate;
    var onScreen = true;
    var timers = [];
    var advance = null;

    function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
    function stopTimers() { timers.forEach(clearTimeout); timers = []; clearTimeout(advance); advance = null; }
    function keysOf(slide) { return slide.getAttribute('data-hits').split(' '); }
    function hit(key) {
      bars.classList.add('focus');
      Object.keys(cards).forEach(function (name) { cards[name].classList.toggle('hit', name === key); });
    }
    function rise(el, delay) {
      el.animate([{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }],
        { duration: 480, delay: delay, easing: EASE, fill: 'backwards' });
    }
    function type(el, delay) {
      el.animate([{ clipPath: 'inset(0 100% 0 0)' }, { clipPath: 'inset(0 0 0 0)' }],
        { duration: 900, delay: delay, easing: 'steps(26, end)', fill: 'backwards' });
    }
    function settle() {
      stopTimers();
      var slide = slides[index];
      if (slide.getAnimations) { slide.getAnimations({ subtree: true }).forEach(function (a) { a.finish(); }); }
      var keys = keysOf(slide);
      hit(keys[keys.length - 1]);
    }
    function queue(ms) {
      clearTimeout(advance);
      advance = null;
      if (auto && onScreen) { advance = setTimeout(function () { go(index + 1, true); }, ms); }
    }
    function go(next, animate) {
      stopTimers();
      index = (next + slides.length) % slides.length;
      slides.forEach(function (slide, i) { slide.classList.toggle('on', i === index); });
      dots.forEach(function (dot, i) { dot.setAttribute('aria-current', i === index ? 'true' : 'false'); });
      var slide = slides[index];
      var keys = keysOf(slide);
      var end = 0;
      if (animate && canAnimate) {
        var rows = $$('.sess-rows > div', slide);
        rows.forEach(function (row, i) { rise(row, START_MS + i * ROW_MS); });
        $$('[data-type]', slide).forEach(function (el) { type(el, START_MS + 2 * ROW_MS); });
        end = START_MS + rows.length * ROW_MS + 700;
        rise($('.sess-verdict', slide), end);
        bars.classList.remove('focus');
        keys.forEach(function (key, i) { later(function () { hit(key); }, end + 450 + i * HIT_GAP_MS); });
        end += 450 + (keys.length - 1) * HIT_GAP_MS;
      } else {
        hit(keys[keys.length - 1]);
      }
      queue(end + DWELL_MS);
    }
    function step(delta) { settle(); go(index + delta, canAnimate); }

    $('[data-prev]', stage).addEventListener('click', function () { step(-1); });
    $('[data-next]', stage).addEventListener('click', function () { step(1); });
    dots.forEach(function (dot, i) { dot.addEventListener('click', function () { settle(); go(i, canAnimate); }); });
    play.addEventListener('click', function () {
      auto = !auto;
      play.setAttribute('data-state', auto ? 'playing' : 'paused');
      play.setAttribute('aria-label', auto ? 'Pause the sample runs' : 'Play the sample runs');
      settle();
      if (auto) { go(index + 1, true); }
    });
    play.hidden = !canAnimate;
    $('.stage-ctl', stage).hidden = false;
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        var now = entries[0].isIntersecting;
        if (now === onScreen) { return; }
        onScreen = now;
        if (now) { queue(RESUME_MS); } else { settle(); }
      }, { threshold: 0.15 }).observe(stage);
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { settle(); } else { queue(RESUME_MS); }
    });
    queue(FIRST_DWELL_MS);
  }

  function navSpy() {
    if (!('IntersectionObserver' in window)) { return; }
    var links = $$('.links a');
    var byId = {};
    links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        links.forEach(function (a) { a.removeAttribute('aria-current'); });
        if (byId[entry.target.id]) { byId[entry.target.id].setAttribute('aria-current', 'true'); }
      });
    }, { rootMargin: '-35% 0px -55% 0px' });
    ['hero', 'results', 'switches', 'why', 'how', 'verdicts', 'run', 'cta'].forEach(function (id) {
      io.observe(document.getElementById(id));
    });
  }

  function verdictFigure() {
    var fig = $('[data-vfig]');
    var caption = $('[data-vcap]', fig);
    var live = $('[data-vlive]', fig);
    var rest = caption.textContent;
    var rows = $$('[data-vrow]');
    var pinned = '';
    function captionOf(name) {
      var found = '';
      rows.forEach(function (row) { if (row.getAttribute('data-vrow') === name) { found = row.getAttribute('data-cap'); } });
      return found;
    }
    function preview(name) {
      fig.setAttribute('data-sel', name);
      caption.textContent = captionOf(name) || rest;
    }
    function pin(name) {
      pinned = name;
      rows.forEach(function (row) { row.setAttribute('aria-pressed', row.getAttribute('data-vrow') === name ? 'true' : 'false'); });
      preview(name);
      live.textContent = captionOf(name);
    }
    function wire(el, name) {
      el.addEventListener('click', function () { pin(pinned === name ? '' : name); });
      el.addEventListener('mouseenter', function () { preview(name); });
      el.addEventListener('mouseleave', function () { preview(pinned); });
    }
    rows.forEach(function (row) { wire(row, row.getAttribute('data-vrow')); });
    $$('.wg', fig).forEach(function (group) { wire(group, group.getAttribute('data-v')); });
    if (canAnimate && !onFirstScreen(fig)) {
      whenVisible([fig], function () {
        $$('.ws i', fig).forEach(function (square, i) {
          square.animate([{ opacity: 0, transform: 'scale(0.4)' }, { opacity: 1, transform: 'none' }],
            { duration: 420, delay: Math.min(i * 14, 700), easing: EASE, fill: 'backwards' });
        });
      }, { rootMargin: '0px', threshold: 0.3 });
    }
  }

  function switchCards() {
    if (!canAnimate) { return; }
    $$('.cards .card').forEach(function (card) {
      if (onFirstScreen(card)) { return; }
      whenVisible([card], function () {
        $('.strike', card).animate([{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], { duration: 520, delay: 450, easing: EASE, fill: 'backwards' });
        $('.arrow', card).animate([{ transform: 'scaleY(0)' }, { transform: 'scaleY(1)' }], { duration: 420, delay: 900, easing: EASE, fill: 'backwards' });
        $('.swap .to', card).animate([{ opacity: 0.1, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], { duration: 600, delay: 1200, easing: EASE, fill: 'backwards' });
      }, { rootMargin: '0px', threshold: 0.4 });
    });
  }

  function chartStory() {
    var story = $('[data-story]');
    if (!('IntersectionObserver' in window)) { return; }
    var wide = window.matchMedia('(min-width: 66rem)');
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting && wide.matches) { story.setAttribute('data-step', entry.target.getAttribute('data-step-id')); }
      });
    }, { rootMargin: '-45% 0px -45% 0px' });
    $$('[data-step-id]', story).forEach(function (step) { io.observe(step); });
    new IntersectionObserver(function (entries) {
      if (!entries[0].isIntersecting) { story.removeAttribute('data-step'); }
    }, { threshold: 0 }).observe(story);
  }

  [reveals, countUps, growChartBars, growHeroBars, copyButtons, menu, rulerProgress, dimOtherLines,
    carousel, navSpy, verdictFigure, switchCards, chartStory].forEach(function (init) { init(); });
})();
