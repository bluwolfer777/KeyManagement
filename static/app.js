(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Righe di tabella cliccabili
  $$("tr[data-href]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a, button, input, form")) return;
      window.location = tr.dataset.href;
    });
  });

  // Conferma prima di azioni distruttive
  $$("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  $$("[data-print]").forEach((b) => b.addEventListener("click", () => window.print()));

  // Filtro testuale su elenchi
  $$("input[data-filter]").forEach((input) => {
    const items = $$(input.dataset.filter);
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      items.forEach((el) => {
        const text = (el.dataset.text || el.textContent).toLowerCase();
        el.hidden = q !== "" && !text.includes(q);
      });
    });
  });

  // Selettore tipo chiave (nuova chiave)
  const kindRadios = $$("input[name=kind]");
  if (kindRadios.length) {
    const update = () => {
      const kind = (kindRadios.find((r) => r.checked) || {}).value;
      $$("[data-kind]").forEach((el) => { el.hidden = el.dataset.kind !== kind; });
    };
    kindRadios.forEach((r) => r.addEventListener("change", update));
    update();
  }

  // Pulsanti +/- per le quantità
  function clamp(input, v) {
    const min = Number(input.min || 0), max = input.max === "" ? Infinity : Number(input.max);
    return Math.max(min, Math.min(max, isNaN(v) ? 0 : v));
  }
  $$(".stepper").forEach((st) => {
    const input = $("input", st);
    $$("[data-step]", st).forEach((b) => b.addEventListener("click", () => {
      input.value = clamp(input, Number(input.value) + Number(b.dataset.step));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }));
    input.addEventListener("change", () => {
      input.value = clamp(input, Number(input.value));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });

  // Riquadro firma
  $$(".sigpad").forEach((wrap) => {
    const canvas = $("canvas", wrap);
    const input = document.getElementById(wrap.dataset.input);
    const ctx = canvas.getContext("2d");
    let drawing = false, dirty = false, last = null, width = 0;

    function setup() {
      const r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
      width = r.width;
      canvas.width = Math.round(r.width * d);
      canvas.height = Math.round(r.height * d);
      ctx.setTransform(d, 0, 0, d, 0, 0);
      ctx.lineWidth = 2.4; ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.strokeStyle = ctx.fillStyle = "#111";
    }
    function save() {
      input.value = dirty ? canvas.toDataURL("image/png") : "";
      wrap.classList.toggle("signed", dirty);
    }
    function restore(dataUrl) {
      const img = new Image();
      img.onload = () => {
        const r = canvas.getBoundingClientRect();
        ctx.drawImage(img, 0, 0, r.width, r.height);
        dirty = true; save();
      };
      img.src = dataUrl;
    }
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

    setup();
    if (input.value) restore(input.value);

    canvas.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* puntatore già rilasciato */ }
      drawing = true; dirty = true; last = pos(e);
      ctx.beginPath(); ctx.arc(last.x, last.y, 1.2, 0, Math.PI * 2); ctx.fill();
      wrap.classList.add("signed");
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!drawing) return;
      const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      ctx.beginPath(); ctx.moveTo(last.x, last.y);
      (events.length ? events : [e]).forEach((ev) => { last = pos(ev); ctx.lineTo(last.x, last.y); });
      ctx.stroke();
    });
    const end = () => { if (drawing) { drawing = false; save(); } };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    $(".sig-clear", wrap).addEventListener("click", () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      dirty = false; save();
    });
    window.addEventListener("resize", () => {
      if (canvas.getBoundingClientRect().width === width) return;
      const data = dirty ? canvas.toDataURL("image/png") : null;
      setup();
      if (data) restore(data);
    });
  });

  // Nuovo prestito: riepilogo, suggerimenti utente, controlli
  const loanForm = $("#loan-form");
  if (loanForm) {
    const summary = $("#loan-summary");
    const numInputs = $$("input[data-num]", loanForm);
    const isOut = (input) => {
      const v = input.value.trim().toLowerCase();
      return v !== "" && input.dataset.out.split("|").some((n) => n !== "" && n.toLowerCase() === v);
    };
    const refresh = () => {
      const parts = [];
      let count = 0;
      $$(".keyrow", loanForm).forEach((row) => {
        let n = 0, label = "";
        const box = $("input[data-key]", row);
        if (box && box.checked) { n = 1; label = row.dataset.name; }
        const num = $("input[data-num]", row);
        if (num) {
          const v = num.value.trim();
          if (v) { n = 1; label = `${row.dataset.name} n. ${v}`; }
          const out = isOut(num);
          num.parentElement.classList.toggle("is-out", out);
          $(".num-warn", row).hidden = !out;
        }
        row.classList.toggle("selected", n > 0);
        if (n > 0) { parts.push(label); count += n; }
      });
      summary.textContent = count ? `${count} ${count === 1 ? "chiave" : "chiavi"}: ${parts.join("; ")}` : "Nessuna chiave selezionata";
    };
    loanForm.addEventListener("input", refresh);
    loanForm.addEventListener("change", refresh);
    refresh();

    loanForm.addEventListener("submit", (e) => {
      const any = $$("input[data-key]:checked", loanForm).length > 0 ||
                  numInputs.some((i) => i.value.trim() !== "");
      if (!any) { e.preventDefault(); alert("Seleziona almeno una chiave."); return; }
      const busy = numInputs.find(isOut);
      if (busy) { e.preventDefault(); alert(`Il numero ${busy.value.trim()} risulta già in prestito.`); busy.focus(); return; }
      if (!$("#signature").value) { e.preventDefault(); alert("Manca la firma di chi ritira le chiavi."); $(".sigpad").scrollIntoView({ behavior: "smooth", block: "center" }); }
    });

    // Suggerimenti utenti già registrati: un tocco compila cognome, nome e contatti
    const first = $("#first-name"), last = $("#last-name"), box = $("#suggest");
    let timer = null, seq = 0;
    const hide = () => { box.hidden = true; box.replaceChildren(); };
    const lookup = () => {
      clearTimeout(timer);
      const q = `${last.value.trim()} ${first.value.trim()}`.trim();
      if (q.length < 2) { hide(); return; }
      const mine = ++seq;
      timer = setTimeout(() => {
        fetch(`${box.dataset.url}?q=${encodeURIComponent(q)}`, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : []))
          .then((data) => {
            if (mine !== seq) return;
            const exact = data.length === 1 && data[0].last_name.toLowerCase() === last.value.trim().toLowerCase() &&
                          data[0].first_name.toLowerCase() === first.value.trim().toLowerCase();
            if (!data.length || exact) { hide(); return; }
            const title = document.createElement("div");
            title.className = "suggest-title";
            title.textContent = "Utenti già registrati:";
            box.replaceChildren(title, ...data.map((b) => {
              const btn = document.createElement("button");
              btn.type = "button";
              const strong = document.createElement("strong");
              strong.textContent = `${b.last_name} ${b.first_name}`;
              const extra = [b.email, b.phone, b.out ? `${b.out} in prestito` : ""].filter(Boolean).join(" · ");
              btn.append(strong);
              if (extra) { const s = document.createElement("span"); s.className = "muted small"; s.textContent = ` — ${extra}`; btn.append(s); }
              btn.addEventListener("click", () => {
                last.value = b.last_name; first.value = b.first_name;
                const email = $("#borrower-email"), phone = $("#borrower-phone");
                if (!email.value) email.value = b.email;
                if (!phone.value) phone.value = b.phone;
                hide();
              });
              return btn;
            }));
            box.hidden = false;
          })
          .catch(() => {});
      }, 200);
    };
    first.addEventListener("input", lookup);
    last.addEventListener("input", lookup);
  }

  // Riconsegna: tutte / nessuna e riepilogo
  const returnForm = $("#return-form");
  if (returnForm) {
    const inputs = $$("input[data-ret]", returnForm);
    const summary = $("#return-summary");
    const refresh = () => {
      let count = 0, total = 0;
      inputs.forEach((i) => {
        if (i.type === "checkbox") { total += 1; if (i.checked) count += 1; }
        else { total += Number(i.max); count += Number(i.value) || 0; }
        i.closest(".keyrow").classList.toggle("selected", i.type === "checkbox" ? i.checked : Number(i.value) > 0);
      });
      summary.textContent = count ? `Riconsegna ${count} di ${total} ${total === 1 ? "chiave" : "chiavi"}` +
        (count === total ? " (tutte)" : "") : "Nessuna chiave selezionata";
    };
    const setAll = (on) => {
      inputs.forEach((i) => { if (i.type === "checkbox") i.checked = on; else i.value = on ? i.max : 0; });
      refresh();
    };
    $("[data-select-all]", returnForm).addEventListener("click", () => setAll(true));
    $("[data-select-none]", returnForm).addEventListener("click", () => setAll(false));
    returnForm.addEventListener("input", refresh);
    returnForm.addEventListener("change", refresh);
    returnForm.addEventListener("submit", (e) => {
      if (!inputs.some((i) => (i.type === "checkbox" ? i.checked : Number(i.value) > 0))) {
        e.preventDefault(); alert("Seleziona almeno una chiave da riconsegnare.");
      }
    });
    refresh();
  }

  // Intervallo di date dello storico: calendario al tocco, limiti coerenti, pulsante per togliere le date
  $$("[data-daterange]").forEach((range) => {
    const [from, to] = $$("input[type=date]", range);
    const sync = () => { to.min = from.value || ""; from.max = to.value || ""; };
    [from, to].forEach((input) => {
      input.addEventListener("change", sync);
      input.closest(".date-field").addEventListener("click", () => {
        try { input.showPicker(); } catch (_) { input.focus(); }
      });
    });
    const clear = $("[data-date-clear]", range);
    if (clear) clear.addEventListener("click", () => { from.value = ""; to.value = ""; range.closest("form").submit(); });
    sync();
  });

  // --- Cookie: consenso e preferenza del tema --------------------------------------
  const YEAR = 60 * 60 * 24 * 365;
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  };
  const setCookie = (name, value, maxAge) => {
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${name}=${encodeURIComponent(value)}; Max-Age=${maxAge}; Path=/; SameSite=Lax${secure}`;
  };
  const deleteCookie = (name) => setCookie(name, "", 0);

  const banner = $("#cookie-banner");
  $$("[data-consent]", banner).forEach((b) => b.addEventListener("click", () => {
    const choice = b.dataset.consent;
    setCookie("gc_consent", choice, YEAR);
    const mode = document.documentElement.dataset.theme;
    if (choice === "all" && mode) setCookie("gc_theme", mode, YEAR);
    if (choice !== "all") deleteCookie("gc_theme");
    banner.hidden = true;
  }));
  $$("[data-cookie-settings]").forEach((b) => b.addEventListener("click", () => { banner.hidden = false; }));

  // Tema: automatico (come il PC) -> scuro -> chiaro -> automatico
  const toggle = $("#theme-toggle");
  const LABELS = { auto: "Tema: automatico (come il PC)", dark: "Tema: scuro", light: "Tema: chiaro" };
  const NEXT = { auto: "dark", dark: "light", light: "auto" };
  const applyMode = (mode) => {
    if (mode === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    toggle.dataset.mode = mode;
    toggle.title = LABELS[mode] + " — tocca per cambiare";
    toggle.setAttribute("aria-label", toggle.title);
  };
  applyMode(toggle.dataset.mode || "auto");
  toggle.addEventListener("click", () => {
    const mode = NEXT[toggle.dataset.mode] || "auto";
    applyMode(mode);
    if (mode === "auto") deleteCookie("gc_theme");
    else if (getCookie("gc_consent") === "all") setCookie("gc_theme", mode, YEAR);
  });
})();
