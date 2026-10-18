// HomeReel theme engine (1.89).
//
// This file is the one registry of themes. Everything else keys off it:
//   - themes.css styles each id under html[data-theme="<id>"];
//   - main.py reads the `id: "..."` lines below at startup to validate what a
//     profile or the household default may be set to (keep one per line);
//   - the Settings theme gallery is rendered from THEMES.
//
// It loads in <head>, before style.css paints anything, so the saved theme is
// on <html> before first paint instead of flashing the default for a frame.
// The theme itself is stored per profile on the server; localStorage only
// caches the last one used on this browser so that first paint is right.
(function () {
  "use strict";

  const THEMES = [
    {
      id: "default",
      name: "HomeReel",
      tagline: "The house style — frosted glass over a teal glow.",
      scheme: "dark",
      swatch: ["#06090c", "#1a2227", "#00c2a8", "#3878ff"],
      fx: null,
    },
    {
      id: "particles",
      name: "Particles",
      tagline: "Drifting motes and soft ambient light, artwork first.",
      scheme: "dark",
      swatch: ["#05060b", "#151826", "#ffb86b", "#7aa2ff"],
      fx: "particles",
    },
    {
      id: "liquid",
      name: "Liquid Morph",
      tagline: "Slow-moving colour blobs and pebble-smooth surfaces.",
      scheme: "dark",
      swatch: ["#0b0820", "#211a46", "#ff7ac6", "#5ee7ff"],
      fx: "liquid",
    },
    {
      id: "neon",
      name: "Neon Glow",
      tagline: "Black glass, cyan and magenta tubes, everything lit.",
      scheme: "dark",
      swatch: ["#04020a", "#120a24", "#00f0ff", "#ff2bd6"],
      fx: null,
    },
    {
      id: "chrome",
      name: "Y2K Chrome",
      tagline: "Brushed silver, glossy pills, millennium futurism.",
      scheme: "dark",
      swatch: ["#0d1117", "#9aa6b2", "#d8e4f0", "#6ad4ff"],
      fx: null,
    },
    {
      id: "kinetic",
      name: "Kinetic Type",
      tagline: "Huge headlines that move; everything else stays quiet.",
      scheme: "dark",
      swatch: ["#0a0a0a", "#1a1a1a", "#d7ff3a", "#ffffff"],
      fx: "kinetic",
    },
    {
      id: "paper",
      name: "Paper Cutout",
      tagline: "Layered card stock, torn edges and collage shadows.",
      scheme: "light",
      swatch: ["#efe6d6", "#fbf6ec", "#e0533d", "#2f6f73"],
      fx: null,
    },
    {
      id: "holo",
      name: "Holographic",
      tagline: "Iridescent foil that shifts as you move across it.",
      scheme: "dark",
      swatch: ["#0c0d18", "#1d1f33", "#b8a4ff", "#7fffd4"],
      fx: "holo",
    },
    {
      id: "isometric",
      name: "Isometric",
      tagline: "Extruded tiles on a 30° grid, depth from stacked shadows.",
      scheme: "dark",
      swatch: ["#10162a", "#1d2742", "#ffcf5a", "#53d1ff"],
      fx: null,
    },
    {
      id: "ascii",
      name: "ASCII Art",
      tagline: "A phosphor terminal. Box-drawing borders, one font.",
      scheme: "dark",
      swatch: ["#050805", "#0c140c", "#7dff8a", "#d9ffb0"],
      fx: null,
    },
    {
      id: "clay",
      name: "Clay 3D",
      tagline: "Soft, puffy, squeezable — every control looks pressable.",
      scheme: "light",
      swatch: ["#e9e3f5", "#f6f2fc", "#7c5cff", "#ff9a8b"],
      fx: null,
    },
    {
      id: "vhs",
      name: "Retro VHS",
      tagline: "Tracking lines, a blinking PLAY and colour bleed.",
      scheme: "dark",
      swatch: ["#0a0a12", "#16162a", "#ff3b6b", "#3bf0ff"],
      fx: "vhs",
    },
    {
      id: "comic",
      name: "Comic Book",
      tagline: "Ink outlines, halftone dots and shouty headings.",
      scheme: "light",
      swatch: ["#fff6dc", "#ffffff", "#e8202a", "#1f6feb"],
      fx: null,
    },
    {
      id: "splitflap",
      name: "Split-Flap",
      tagline: "A railway departures board — titles flip into place.",
      scheme: "dark",
      swatch: ["#0e0e0e", "#1c1c1c", "#ffc531", "#f2f2f2"],
      fx: "flap",
    },
    {
      id: "sketch",
      name: "Hand-Drawn",
      tagline: "Pencil on notebook paper, wobbly boxes and doodles.",
      scheme: "light",
      swatch: ["#fbfaf4", "#ffffff", "#2b59c3", "#e2574c"],
      fx: null,
    },
    {
      id: "blueprint",
      name: "Blueprint",
      tagline: "Drafting grid, white linework, dimension callouts.",
      scheme: "dark",
      swatch: ["#0b2a5b", "#103774", "#e9f4ff", "#7fd3ff"],
      fx: null,
    },
    {
      id: "physics",
      name: "Physics",
      tagline: "Springs and inertia — things settle, squash and bounce.",
      scheme: "dark",
      swatch: ["#0d1020", "#1a1f38", "#ff6b4a", "#6be0ff"],
      fx: "physics",
    },
    {
      id: "pixel",
      name: "Pixel Art",
      tagline: "8-bit chunky borders and a console palette.",
      scheme: "dark",
      swatch: ["#1a1c2c", "#333c57", "#ffcd75", "#38b764"],
      fx: null,
    },
    {
      id: "bauhaus",
      name: "Bauhaus",
      tagline: "Circle, square, triangle. Primary colours, strict grid.",
      scheme: "light",
      swatch: ["#f2ede3", "#ffffff", "#d7261e", "#1d4fa3"],
      fx: null,
    },
    {
      id: "deco",
      name: "Art Deco",
      tagline: "Gold leaf on black lacquer, fan motifs, stepped frames.",
      scheme: "dark",
      swatch: ["#0b0b0d", "#17161a", "#d4af37", "#f3e5ab"],
      fx: null,
    },
    {
      id: "brutal",
      name: "Neo-Brutalism",
      tagline: "Thick black borders, hard offset shadows, loud fills.",
      scheme: "light",
      swatch: ["#fff4d6", "#ffffff", "#ff5c8a", "#4d7cff"],
      fx: null,
    },
  ];

  const STORE_THEME = "homereel.theme";
  const STORE_FX = "homereel.effects";   // "off" = decorative motion disabled on this device
  const byId = new Map(THEMES.map((t) => [t.id, t]));
  const root = document.documentElement;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function store(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch { /* private mode / blocked storage: the server copy still holds it */ }
  }
  function read(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  // ---- effects -----------------------------------------------------------
  // Decorative only: each one is pointer-events:none, aria-hidden, and never
  // runs under prefers-reduced-motion or with effects switched off.

  let fxTeardown = null;

  function motionAllowed() {
    return !reducedMotion.matches && read(STORE_FX) !== "off";
  }

  function fxLayer(kind) {
    const el = document.createElement("div");
    el.className = `theme-fx theme-fx-${kind}`;
    el.setAttribute("aria-hidden", "true");
    document.body.prepend(el);
    return el;
  }

  // Slow drifting points of light on a canvas behind everything. Capped
  // count, pauses when the tab is hidden, and redraws at the device's pixel
  // ratio so motes stay crisp without costing a big backing store.
  function particles() {
    const layer = fxLayer("particles");
    const canvas = document.createElement("canvas");
    layer.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = 0, h = 0, raf = 0, motes = [];
    const hues = [32, 38, 220, 260];

    function resize() {
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.min(70, Math.round((w * h) / 26000));
      motes = Array.from({ length: count }, () => ({
        x: Math.random() * w, y: Math.random() * h,
        r: 0.6 + Math.random() * 2.2,
        vx: (Math.random() - 0.5) * 0.12, vy: -0.05 - Math.random() * 0.22,
        a: 0.15 + Math.random() * 0.55, tw: Math.random() * Math.PI * 2,
        hue: hues[(Math.random() * hues.length) | 0],
      }));
    }
    function frame() {
      ctx.clearRect(0, 0, w, h);
      for (const m of motes) {
        m.x += m.vx; m.y += m.vy; m.tw += 0.02;
        if (m.y < -10) { m.y = h + 10; m.x = Math.random() * w; }
        if (m.x < -10) m.x = w + 10; else if (m.x > w + 10) m.x = -10;
        const alpha = m.a * (0.65 + 0.35 * Math.sin(m.tw));
        const g = ctx.createRadialGradient(m.x, m.y, 0, m.x, m.y, m.r * 4);
        g.addColorStop(0, `hsla(${m.hue}, 100%, 78%, ${alpha})`);
        g.addColorStop(1, `hsla(${m.hue}, 100%, 70%, 0)`);
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(m.x, m.y, m.r * 4, 0, Math.PI * 2); ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    }
    function visibility() {
      cancelAnimationFrame(raf);
      if (!document.hidden) raf = requestAnimationFrame(frame);
    }
    resize();
    raf = requestAnimationFrame(frame);
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", visibility);
      layer.remove();
    };
  }

  // CSS does the moving (see .theme-fx-liquid in themes.css); JS only mounts it.
  function liquid() {
    const layer = fxLayer("liquid");
    layer.innerHTML = "<i></i><i></i><i></i>";
    return () => layer.remove();
  }

  // On-screen display in the corner, like a deck's: PLAY and a running
  // counter. The counter is the wall clock, so it reads as a real timestamp.
  function vhs() {
    const layer = fxLayer("vhs");
    layer.innerHTML = '<span class="vhs-osd-play">PLAY &#9654;</span><span class="vhs-osd-time"></span>';
    const time = layer.querySelector(".vhs-osd-time");
    const tick = () => {
      const d = new Date();
      const p = (n) => String(n).padStart(2, "0");
      time.textContent = `SP  ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { clearInterval(id); layer.remove(); };
  }

  // Splits headings into per-character (flap) or per-word (kinetic) spans so
  // CSS can stagger an animation across them. The heading keeps its text as
  // an aria-label and the spans are aria-hidden, so screen readers still read
  // one word, not a letter at a time. app.js keeps re-rendering these
  // headings with textContent; the observer re-splits whatever it writes.
  const SPLIT_SELECTOR = ".tab-hero-title, .page-title, .detail-page-title, .shelf-block h2";

  function splitter(mode) {
    function split(el) {
      if (el.dataset.split === el.textContent && el.querySelector(".fx-ch")) return;
      const text = el.textContent;
      if (!text.trim()) return;
      const frag = document.createDocumentFragment();
      let i = 0;
      const unit = (part) => {
        const span = document.createElement("span");
        span.className = "fx-ch";
        span.setAttribute("aria-hidden", "true");
        span.style.setProperty("--i", i++);
        span.textContent = part;
        return span;
      };
      for (const word of text.split(/(\s+)/)) {
        if (!word) continue;
        if (/^\s+$/.test(word)) { frag.appendChild(document.createTextNode(word)); continue; }
        if (mode === "word") { frag.appendChild(unit(word)); continue; }
        // Per-character, but each word's letters stay in one unbreakable box,
        // or a line could wrap between two letters of the same word.
        const box = document.createElement("span");
        box.className = "fx-word";
        box.setAttribute("aria-hidden", "true");
        for (const ch of Array.from(word)) box.appendChild(unit(ch));
        frag.appendChild(box);
      }
      el.setAttribute("aria-label", text);
      el.textContent = "";
      el.appendChild(frag);
      el.dataset.split = text;   // the joined spans read back as the same text
    }
    function scan(node) {
      if (node.nodeType !== 1) return;
      if (node.matches(SPLIT_SELECTOR)) split(node);
      node.querySelectorAll(SPLIT_SELECTOR).forEach(split);
    }
    scan(document.body);
    const obs = new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "characterData") {
          const host = r.target.parentElement?.closest(SPLIT_SELECTOR);
          if (host) split(host);
          continue;
        }
        const host = r.target.closest?.(SPLIT_SELECTOR);
        if (host) { split(host); continue; }
        r.addedNodes.forEach(scan);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    return () => {
      obs.disconnect();
      document.querySelectorAll("[data-split]").forEach((el) => {
        el.textContent = el.dataset.split;
        el.removeAttribute("data-split");
        el.removeAttribute("aria-label");
      });
    };
  }

  // Pointer position as CSS variables on whatever card/panel is under it, so
  // the foil sheen (holo) or tilt (physics) can follow the cursor. One
  // passive listener for the whole page; rAF-throttled.
  function pointerVars(selector) {
    let raf = 0, last = null;
    function move(e) {
      last = e;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const el = last.target.closest?.(selector);
        if (!el) return;
        const r = el.getBoundingClientRect();
        el.style.setProperty("--mx", ((last.clientX - r.left) / r.width).toFixed(3));
        el.style.setProperty("--my", ((last.clientY - r.top) / r.height).toFixed(3));
      });
    }
    function leave(e) {
      const el = e.target.closest?.(selector);
      if (el && !el.contains(e.relatedTarget)) {
        el.style.removeProperty("--mx");
        el.style.removeProperty("--my");
      }
    }
    document.addEventListener("pointermove", move, { passive: true });
    document.addEventListener("pointerout", leave, { passive: true });
    return () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerout", leave);
      cancelAnimationFrame(raf);
    };
  }

  const FX = {
    particles,
    liquid,
    vhs,
    flap: () => splitter("char"),
    kinetic: () => splitter("word"),
    holo: () => pointerVars(".card, .tab-hero, .panel"),
    physics: () => pointerVars(".card"),
  };

  function startFx(theme) {
    if (fxTeardown) { fxTeardown(); fxTeardown = null; }
    root.dataset.fx = motionAllowed() ? "on" : "off";
    if (!theme.fx || !motionAllowed() || !document.body) return;
    fxTeardown = FX[theme.fx]();
  }

  // ---- applying ------------------------------------------------------------

  let current = byId.get(read(STORE_THEME)) || byId.get("default");

  function setMetaColor(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme.swatch[0];
  }

  function apply(id, { persist = true } = {}) {
    const theme = byId.get(id) || byId.get("default");
    current = theme;
    root.dataset.theme = theme.id;
    root.style.colorScheme = theme.scheme;
    setMetaColor(theme);
    if (persist) store(STORE_THEME, theme.id);
    if (document.body) startFx(theme);
    return theme;
  }

  function setEffects(enabled) {
    store(STORE_FX, enabled ? null : "off");
    startFx(current);
  }

  // Before first paint: the cached theme, so the page draws in it at once.
  apply(current.id, { persist: false });
  document.addEventListener("DOMContentLoaded", () => { setMetaColor(current); startFx(current); });
  reducedMotion.addEventListener?.("change", () => startFx(current));

  window.HomeReelThemes = {
    list: THEMES,
    get: (id) => byId.get(id),
    get current() { return current.id; },
    // The id this browser last used, or null on one that has never signed in.
    cached: () => (byId.has(read(STORE_THEME)) ? read(STORE_THEME) : null),
    apply,
    effectsEnabled: () => read(STORE_FX) !== "off",
    reducedMotion: () => reducedMotion.matches,
    setEffects,
  };
})();
