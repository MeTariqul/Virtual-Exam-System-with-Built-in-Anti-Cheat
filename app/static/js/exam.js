/* Virtual Exam System — client-side exam runner & anti-cheat module.
 *
 * Design rules (see plan.md):
 *  - NEVER auto-submits because of a violation. Violations are proof: they are
 *    stored, the student sees an alert, the teacher gets a live alert.
 *  - The timer only counts down to the server deadline; submissions are
 *    validated server-side.
 *  - Every event is debounced so one Alt-Tab does not spam the log.
 *  - Typed answers are normal typing: only copy/cut/paste and shortcuts are
 *    blocked (so students cannot pull text in from elsewhere).
 */
(function () {
  "use strict";

  const payload = window.EXAM_PAYLOAD;
  if (!payload) return;

  const ATTEMPT = payload.attempt_id;
  const LIMIT = payload.violation_limit || 3;
  const SERVER_OFFSET = new Date(payload.server_time).getTime() - Date.now();
  const DEADLINE = new Date(payload.deadline).getTime();
  const GRACE_MS = 2000; // matches GRACE_WINDOW_MS on the server

  const state = {
    // {questionId: displayIndex | number[] | string}
    answers: Object.assign({}, payload.answers),
    textAnswers: {},
    submitted: false,
    warningCount: payload.warning_count || 0,
    overLimit: payload.over_limit || false,
    saveTimer: null,
    dirty: false,
    focusStartedAt: null,
    lastViolationAt: {},   // type -> timestamp (debounce)
    fullscreenOk: !payload.require_fullscreen,
    webcamOn: false,
    webcamStream: null,
    faceApi: null,
    faceCheckTried: false,
    oneAtATime: !!payload.one_at_a_time,
    current: 1,            // current position in one-at-a-time mode
    began: !payload.one_at_a_time,
  };

  // ------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Focus trap for modal dialogs (fullscreen/consent/intro overlays)
  function trapFocus(element) {
    const focusable = element.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    if (!focusable.length) return () => {};
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    function handler(e) {
      if (e.key !== "Tab") return;
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    element.addEventListener("keydown", handler);
    first.focus();
    return () => element.removeEventListener("keydown", handler);
  }

  function csrfHeaders(extra) {
    const headers = Object.assign(
      { "X-CSRFToken": (document.querySelector('meta[name="csrf-token"]') || {}).content || "" },
      extra || {}
    );
    return headers;
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || "Request failed");
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------
  // Alerts (student-facing). The exam keeps running — no auto-submit.
  // ------------------------------------------------------------------
  const banner = $("#alert-banner");
  const bannerText = $("#alert-text");
  let bannerTimer = null;

  function showAlert(text, sticky) {
    if (!banner) return;
    bannerText.textContent = text;
    banner.hidden = false;
    banner.classList.toggle("alert-danger", /limit|monitor/i.test(text));
    clearTimeout(bannerTimer);
    if (!sticky) bannerTimer = setTimeout(() => { banner.hidden = true; }, 6000);
  }
  const alertClose = $("#alert-close");
  if (alertClose) alertClose.addEventListener("click", () => { banner.hidden = true; });

  function updateWarnings(count, over) {
    state.warningCount = count;
    state.overLimit = !!over;
    const el = $("#warning-count");
    if (el) el.textContent = String(count);
    const hud = $("#hud-warnings");
    if (hud) hud.classList.toggle("hud-danger", !!over);
  }

  // ------------------------------------------------------------------
  // Timer (server deadline only; refresh does not help)
  // ------------------------------------------------------------------
  const timerEl = $("#timer");
  const hudTimer = $("#hud-timer");

  function tick() {
    const left = DEADLINE - (Date.now() + SERVER_OFFSET);
    const secs = Math.max(0, Math.floor(left / 1000));
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    const text = `${mm}:${ss}`;
    if (timerEl) timerEl.textContent = text;
    if (hudTimer) hudTimer.textContent = "⏱ " + text;
    if (timerEl) {
      timerEl.classList.toggle("time-danger", secs <= 60);
      timerEl.classList.toggle("time-warn", secs <= 300 && secs > 60);
    }
    if (left <= 0 && !state.submitted) submitExam(true);
  }

  // ------------------------------------------------------------------
  // Answer state helpers (single, multi, typed)
  // ------------------------------------------------------------------
  function isAnswered(qid, card) {
    const value = state.answers[qid];
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === "string") return value.trim().length > 0;
    if (value !== undefined && value !== null) return true;
    const text = state.textAnswers[qid];
    return typeof text === "string" && text.trim().length > 0;
  }

  function mergedPayloadAnswers() {
    // Auto-save / submit payload: {qid: index | [indexes] | "text"}
    const merged = {};
    Object.keys(state.answers).forEach((qid) => {
      merged[qid] = state.answers[qid];
    });
    Object.keys(state.textAnswers).forEach((qid) => {
      merged[qid] = state.textAnswers[qid];
    });
    return merged;
  }

  // ------------------------------------------------------------------
  // Answer saving (debounced auto-save)
  // ------------------------------------------------------------------
  const saveStatus = $("#save-status");

  function markDirty() {
    state.dirty = true;
    if (saveStatus) saveStatus.textContent = "Saving…";
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(flushAnswers, 700);
  }

  async function flushAnswers() {
    if (!state.dirty || state.submitted) return;
    state.dirty = false;
    try {
      await post(`/api/attempts/${ATTEMPT}/answers`, { answers: mergedPayloadAnswers() });
      if (saveStatus && !state.dirty) saveStatus.textContent = "All changes saved.";
    } catch (err) {
      if (err.status === 410) {
        showAlert("Time is up on the server — submitting…", true);
        submitExam(true);
      } else if (err.status === 409) {
        // Attempt already submitted elsewhere (teacher ended it).
        window.location = `/student/attempts/${ATTEMPT}/result`;
      } else {
        if (saveStatus) saveStatus.textContent = "Save failed — retrying…";
        state.dirty = true;
        setTimeout(flushAnswers, 3000);
      }
    }
  }

  function bindQuestions() {
    $$(".question-card").forEach((card) => {
      const qid = card.dataset.questionId;
      const type = card.dataset.type;

      if (type === "multi") {
        const saved = Array.isArray(payload.answers[qid]) ? payload.answers[qid] : null;
        const serverSaved = (payload.questions.find((q) => String(q.id) === String(qid)) || {}).selected;
        const chosen = saved || serverSaved || [];
        if (chosen.length) state.answers[qid] = chosen.slice();
        $$("input[type=checkbox]", card).forEach((input) => {
          if (chosen.indexOf(Number(input.value)) !== -1) input.checked = true;
          input.addEventListener("change", () => {
            const picked = $$("input[type=checkbox]:checked", card)
              .map((el) => Number(el.value))
              .sort((a, b) => a - b);
            state.answers[qid] = picked;
            updateProgress();
            markDirty();
          });
        });
      } else if (type === "mcq" || type === "true_false") {
        // Restore saved selection (legacy single-value shape).
        if (state.answers[qid] !== undefined && state.answers[qid] !== null &&
            !Array.isArray(state.answers[qid]) && typeof state.answers[qid] !== "string") {
          $$("input[type=radio]", card).forEach((input) => {
            if (Number(input.value) === Number(state.answers[qid])) input.checked = true;
          });
        }
        $$("input[type=radio]", card).forEach((input) => {
          input.addEventListener("change", () => {
            state.answers[qid] = Number(input.value);
            updateProgress();
            markDirty();
          });
        });
      } else {
        // Typed answer (short/long text): autosave + word counter.
        const textarea = $("textarea", card);
        if (!textarea) return;
        const limit = Number(textarea.dataset.wordLimit || 300);
        const counter = $(`.word-counter[data-for="${qid}"]`);
        const serverText = (payload.questions.find((q) => String(q.id) === String(qid)) || {}).response || "";
        if (serverText) state.textAnswers[qid] = serverText;

        function refreshCounter() {
          if (!counter) return;
          const words = textarea.value.trim()
            ? textarea.value.trim().split(/\s+/).length : 0;
          counter.textContent = `${words} / ${limit} words`;
          counter.classList.toggle("over-limit", words > limit);
          counter.classList.toggle("near-limit", words > limit * 0.9 && words <= limit);
        }
        refreshCounter();

        textarea.addEventListener("input", () => {
          state.textAnswers[qid] = textarea.value;
          refreshCounter();
          updateProgress();
          markDirty();
        });
        // Paste is blocked everywhere (anti-cheat), including textareas.
      }

      // Restore flag state.
      const flagBtn = $(".flag-btn", card);
      if (flagBtn && card.dataset.flagged === "true") flagBtn.classList.add("is-flagged");
    });
    updateProgress();
  }

  function updateProgress() {
    const cards = $$(".question-card");
    const total = cards.length;
    let done = 0;
    const dots = $$(".nav-dot");
    cards.forEach((card) => {
      const qid = card.dataset.questionId;
      const answered = isAnswered(qid, card);
      const pos = Number(card.dataset.position);
      if (answered) done++;
      const dot = dots.find((d) => d.dataset.target === card.dataset.position);
      if (dot) {
        dot.classList.toggle("is-answered", answered);
        dot.classList.toggle("is-current", state.oneAtATime && pos === state.current);
        const flagged = card.dataset.flagged === "true" ||
          ($(".flag-btn", card) || { classList: { contains: () => false } })
            .classList.contains("is-flagged");
        dot.classList.toggle("is-flagged", flagged);
      }
    });
    const hud = $("#hud-progress");
    if (hud) hud.textContent = `${done}/${total}`;
    const fill = $("#progress-fill");
    if (fill) fill.style.width = total ? `${(done / total) * 100}%` : "0%";
  }

  // ------------------------------------------------------------------
  // Question navigator (jump to a question) & flags
  // ------------------------------------------------------------------
  function bindNavigator() {
    $$(".nav-dot").forEach((dot) => {
      dot.addEventListener("click", () => {
        if (state.oneAtATime) return;
        const card = $(`.question-card[data-position="${dot.dataset.target}"]`);
        if (card) {
          card.scrollIntoView({ behavior: "smooth", block: "center" });
          card.classList.add("flash-highlight");
          setTimeout(() => card.classList.remove("flash-highlight"), 900);
        }
      });
    });

    $$(".flag-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const qid = btn.dataset.questionId;
        const card = btn.closest(".question-card");
        const flagged = !(card && card.dataset.flagged === "true");
        if (card) card.dataset.flagged = flagged ? "true" : "false";
        btn.classList.toggle("is-flagged", flagged);
        // Keep the accessible name/pressed state in sync with the visual one.
        const pos = card ? card.dataset.position : "";
        const label = flagged
          ? `Unflag question ${pos}`
          : `Flag question ${pos} for review`;
        btn.setAttribute("aria-pressed", flagged ? "true" : "false");
        btn.setAttribute("aria-label", label);
        btn.setAttribute("title", label);
        updateProgress();
        post(`/api/attempts/${ATTEMPT}/flag`, { question_id: Number(qid), flagged: flagged })
          .catch(() => {});
      });
    });
  }

  // ------------------------------------------------------------------
  // One-question-at-a-time mode (forward only, no going back)
  // ------------------------------------------------------------------
  function showPosition(position) {
    $$(".question-card").forEach((card) => {
      card.hidden = Number(card.dataset.position) !== position;
    });
    state.current = position;
    const total = $$(".question-card").length;
    const prev = $("#btn-prev");
    if (prev) prev.hidden = true; // proposal: no going back
    const next = $("#btn-next");
    if (next) next.textContent = position >= total ? "Review & submit →" : "Next →";
    const progress = $("#one-progress");
    if (progress) progress.textContent = `Question ${position} of ${total}`;
    // Update navigator dots for one-at-a-time mode
    updateProgress();
  }

  function bindOneAtATime() {
    if (!state.oneAtATime) return;
    const intro = $("#intro-overlay");
    const beginBtn = $("#btn-begin");
    const nav = $("#one-nav");
    let introFocusTrap = null;
    if (beginBtn && intro) {
      beginBtn.addEventListener("click", () => {
        intro.hidden = true;
        if (introFocusTrap) { introFocusTrap(); introFocusTrap = null; }
        state.began = true;
        if (nav) nav.hidden = false;
        showPosition(1);
      });
      intro.hidden = false; // wait for the student to press Begin
      introFocusTrap = trapFocus(intro.querySelector(".fullscreen-box"));
    } else {
      state.began = true;
      if (nav) nav.hidden = false;
      showPosition(1);
    }

    const next = $("#btn-next");
    if (next) {
      next.addEventListener("click", () => {
        const total = $$(".question-card").length;
        if (state.current >= total) {
          if (nav) nav.hidden = true;
          submitExam(false);
          return;
        }
        showPosition(state.current + 1);
      });
    }
  }

  // ------------------------------------------------------------------
  // Violation reporting (debounced per type)
  // ------------------------------------------------------------------
  function currentQuestionId() {
    // One-at-a-time: whatever is on screen. Otherwise the first unanswered,
    // else the first on screen.
    if (state.oneAtATime) {
      const card = $(`.question-card[data-position="${state.current}"]`);
      return card ? Number(card.dataset.questionId) : null;
    }
    const cards = $$(".question-card");
    for (const card of cards) {
      if (!isAnswered(card.dataset.questionId, card)) {
        return Number(card.dataset.questionId);
      }
    }
    return cards.length ? Number(cards[0].dataset.questionId) : null;
  }

  async function reportViolation(type, opts) {
    if (state.submitted) return;
    opts = opts || {};
    const now = Date.now();
    const debounce = opts.debounceMs || 1500;
    if (state.lastViolationAt[type] && now - state.lastViolationAt[type] < debounce) return;
    state.lastViolationAt[type] = now;

    const body = {
      type: type,
      question_id: opts.questionId !== undefined ? opts.questionId : currentQuestionId(),
      duration_ms: opts.durationMs,
      detail: opts.detail,
      snapshot_id: opts.snapshotId,
    };
    try {
      const data = await post(`/api/attempts/${ATTEMPT}/violations`, body);
      if (data && data.ok) {
        updateWarnings(data.warning_count, data.over_limit);
        if (data.message) showAlert(data.message, !!data.over_limit);
      }
    } catch (err) {
      if (err.status === 409) {
        window.location = `/student/attempts/${ATTEMPT}/result`;
      }
      // Other failures: the server records gaps itself via heartbeats.
    }
    // Best-effort evidence photo for the report.
    if (opts.snapshot !== false && state.webcamOn) captureSnapshot("violation");
  }

  // ------------------------------------------------------------------
  // Tab / window focus detection with grace window
  // ------------------------------------------------------------------
  function focusLost() {
    if (state.focusStartedAt === null) state.focusStartedAt = Date.now();
  }
  function focusRegained() {
    if (state.focusStartedAt === null) return;
    const duration = Date.now() - state.focusStartedAt;
    state.focusStartedAt = null;
    if (duration < 100) return; // same flicker, ignore
    reportViolation("tab_switch", { durationMs: duration });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) focusLost();
    else focusRegained();
  });
  window.addEventListener("blur", focusLost);
  window.addEventListener("focus", focusRegained);

  // ------------------------------------------------------------------
  // Fullscreen enforcement
  // ------------------------------------------------------------------
  const fsOverlay = $("#fullscreen-overlay");

  let fsFocusTrap = null;
  function setFullscreenState(inFull) {
    state.fullscreenOk = !payload.require_fullscreen || inFull;
    if (fsOverlay) {
      fsOverlay.hidden = state.fullscreenOk;
      if (state.fullscreenOk && fsFocusTrap) {
        fsFocusTrap();
        fsFocusTrap = null;
      } else if (!state.fullscreenOk && fsOverlay && !fsFocusTrap) {
        fsFocusTrap = trapFocus(fsOverlay.querySelector(".fullscreen-box"));
      }
    }
    if (payload.require_fullscreen && !inFull && !state.submitted) {
      reportViolation("fullscreen_exit", { debounceMs: 2000 });
    }
  }

  document.addEventListener("fullscreenchange", () => {
    setFullscreenState(!!document.fullscreenElement);
  });

  const btnFs = $("#btn-fullscreen");
  if (btnFs) {
    btnFs.addEventListener("click", () => {
      const el = document.documentElement;
      const p = el.requestFullscreen ? el.requestFullscreen() : Promise.resolve();
      Promise.resolve(p).catch(() => {
        showAlert("Fullscreen was blocked by the browser — press F11.", true);
      });
    });
  }

  // ------------------------------------------------------------------
  // Copy / paste / right-click / shortcuts blocking
  // ------------------------------------------------------------------
  const BLOCKED_KEYS = { c: 1, v: 1, x: 1, p: 1, s: 1, u: 1 };

  document.addEventListener("copy", onClipboard);
  document.addEventListener("cut", onClipboard);
  document.addEventListener("paste", onClipboard);

  function onClipboard(e) {
    // Even inside a textarea: typed answers must be typed, not pasted.
    e.preventDefault();
    reportViolation("copy_paste", { detail: { event: e.type } });
  }

  document.addEventListener("contextmenu", (e) => {
    if (e.target.closest("input, textarea")) return; // let the exam UI work
    e.preventDefault();
    reportViolation("copy_paste", { detail: { event: "contextmenu" } });
  });

  document.addEventListener("keydown", (e) => {
    const key = (e.key || "").toLowerCase();
    const inField = !!(e.target && e.target.closest && e.target.closest("input, textarea"));
    if ((e.ctrlKey || e.metaKey) && BLOCKED_KEYS[key]) {
      e.preventDefault();
      reportViolation("copy_paste", { detail: { key: "ctrl+" + key } });
      return;
    }
    // Ctrl+A is allowed inside text fields (students need to edit their own
    // typed answer) but blocked everywhere else.
    if ((e.ctrlKey || e.metaKey) && key === "a" && !inField) {
      e.preventDefault();
      reportViolation("copy_paste", { detail: { key: "ctrl+a" } });
      return;
    }
    if (e.key === "PrintScreen") {
      reportViolation("copy_paste", { detail: { key: "printscreen" } });
    }
    if (e.key === "F12" || (e.ctrlKey && e.shiftKey && (key === "i" || key === "j" || key === "c"))) {
      e.preventDefault();
      reportViolation("copy_paste", { detail: { key: "devtools" } });
    }
  });

  // ------------------------------------------------------------------
  // Webcam: consent, periodic snapshots, face checks (face-api.js)
  // ------------------------------------------------------------------
  const consentBox = $("#webcam-consent");

  async function startWebcam() {
    if (!payload.webcam_enabled || state.webcamOn) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: 320, height: 240, facingMode: "user" },
        audio: false,
      });
      state.webcamStream = stream;
      state.webcamOn = true;
      const video = $("#webcam");
      if (video) video.srcObject = stream;
      const status = $("#webcam-status");
      if (status) status.textContent = "Camera on ✓";
      await post(`/api/attempts/${ATTEMPT}/consent`, { granted: true });
      loadFaceApi();
      setInterval(periodicSnapshot, 45000);
      // First photo right away so the report has a baseline / identity shot.
      setTimeout(() => captureSnapshot("identity"), 2500);
    } catch (err) {
      const status = $("#webcam-status");
      if (status) status.textContent = "Camera unavailable ✗";
      showAlert("Camera could not be started — your teacher has been notified.", false);
      await post(`/api/attempts/${ATTEMPT}/violations`, {
        type: "no_face",
        detail: { reason: "camera_denied" },
      }).catch(() => {});
    }
  }

  function loadFaceApi() {
    // face-api.js from CDN; if it fails, face checks degrade to "unavailable"
    // (recorded once, weight 0 — see BASE_WEIGHTS.face_check_unavailable).
    if (state.faceCheckTried) return;
    state.faceCheckTried = true;
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/dist/face-api.js";
    script.onload = async () => {
      try {
        const MODELS = "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/model/";
        await Promise.all([
          faceapi.nets.tinyFaceDetector.loadFromUri(MODELS),
          faceapi.nets.faceLandmark68Net.loadFromUri(MODELS),
        ]);
        state.faceApi = faceapi;
        const status = $("#webcam-status");
        if (status) status.textContent = "Camera on ✓ face check ready";
      } catch (e) {
        noteFaceUnavailable();
      }
    };
    script.onerror = noteFaceUnavailable;
    document.head.appendChild(script);
  }

  function noteFaceUnavailable() {
    post(`/api/attempts/${ATTEMPT}/violations`, {
      type: "face_check_unavailable",
      detail: { reason: "model_load_failed" },
      question_id: null,
    }).catch(() => {});
  }

  async function countFaces(video) {
    if (!state.faceApi) return -1;
    try {
      const detections = await state.faceApi
        .detectAllFaces(video, new faceapi.TinyFaceDetectorOptions({ inputSize: 160 }))
        .withFaceLandmarks();
      return detections.length;
    } catch (e) {
      return -1;
    }
  }

  async function captureSnapshot(reason) {
    if (!state.webcamOn || state.submitted) return;
    const video = $("#webcam");
    if (!video || !video.videoWidth) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.7);

      const faceCount = await countFaces(video);
      let faceViolation = null;
      if (state.faceApi && faceCount === 0) faceViolation = "no_face";
      else if (state.faceApi && faceCount > 1) faceViolation = "multiple_faces";

      await post(`/api/attempts/${ATTEMPT}/snapshot`, {
        image: dataUrl,
        reason: reason,
        face_count: faceCount,
        face_violation: faceViolation,
      });
      if (faceViolation) {
        showAlert(
          faceViolation === "no_face"
            ? "No face detected — warning sent to your teacher."
            : "More than one face detected — warning sent to your teacher.",
          false
        );
      }
    } catch (e) {
      /* photo failures must never disturb the exam */
    }
  }

  async function periodicSnapshot() {
    if (state.submitted || document.hidden) return; // don't shoot a hidden tab
    await captureSnapshot("periodic");
    // A dark room is a warning, not a violation (see plan.md grace rules).
    const video = $("#webcam");
    if (video && video.videoWidth) {
      try {
        const c = document.createElement("canvas");
        c.width = 32; c.height = 24;
        const ctx = c.getContext("2d");
        ctx.drawImage(video, 0, 0, 32, 24);
        const d = ctx.getImageData(0, 0, 32, 24).data;
        let sum = 0;
        for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
        const avg = sum / (d.length / 4 * 3);
        if (avg < 28) {
          showAlert("Room looks too dark — the camera warning was logged (no penalty).", false);
          post(`/api/attempts/${ATTEMPT}/violations`, {
            type: "low_light", detail: { brightness: Math.round(avg) },
          }).catch(() => {});
        }
      } catch (e) { /* ignore */ }
    }
  }

  // Consent checkbox enables/disables the Continue button
  const btnConsent = $("#btn-consent");
  const consentCheck = $("#consent-check");
  let consentFocusTrap = null;
  if (consentCheck && btnConsent) {
    consentCheck.addEventListener("change", () => {
      btnConsent.disabled = !consentCheck.checked;
    });
    // initial state
    btnConsent.disabled = !consentCheck.checked;
  }

  // Consent buttons
  if (btnConsent) {
    btnConsent.addEventListener("click", async () => {
      if (consentCheck && !consentCheck.checked) {
        const err = $("#consent-error");
        if (err) err.hidden = false;
        return;
      }
      if (consentFocusTrap) { consentFocusTrap(); consentFocusTrap = null; }
      await startWebcam();
      consentBox.hidden = true;
      beginExam();
    });
  }

  // Show consent overlay with focus trap
  if (consentBox && !consentBox.hidden && !consentFocusTrap) {
    consentFocusTrap = trapFocus(consentBox.querySelector(".fullscreen-box"));
  }
  const btnRefuse = $("#btn-refuse");
  if (btnRefuse) {
    btnRefuse.addEventListener("click", async () => {
      await post(`/api/attempts/${ATTEMPT}/consent`, { granted: false }).catch(() => {});
      showAlert("Webcam refused — your teacher has been notified.", false);
      consentBox.hidden = true;
      beginExam();
    });
  }

  // ------------------------------------------------------------------
  // Heartbeat (server detects gaps -> monitor_gap violation)
  // ------------------------------------------------------------------
  async function heartbeat() {
    if (state.submitted) return;
    try {
      const data = await post(`/api/attempts/${ATTEMPT}/heartbeat`, {});
      if (data.status === "submitted") {
        window.location = `/student/attempts/${ATTEMPT}/result`;
      }
    } catch (e) { /* offline: server-side gap logic covers this */ }
  }

  // ------------------------------------------------------------------
  // Live stream: teacher messages, extensions, forced ends
  // ------------------------------------------------------------------
  function connectStream() {
    if (!window.EventSource) return;
    const src = new EventSource(`/api/attempts/${ATTEMPT}/stream`);
    src.onmessage = (msg) => {
      let data;
      try { data = JSON.parse(msg.data); } catch (e) { return; }
      if (data.kind === "alert") {
        updateWarnings(data.warning_count, data.over_limit);
        showAlert(data.message || "Violation recorded.", !!data.over_limit);
        appendTeacherMessage("system", data.message || "Violation recorded.");
      } else if (data.kind === "message") {
        appendTeacherMessage("teacher", data.body);
        showAlert("Teacher: " + data.body, true);
      } else if (data.kind === "extended") {
        appendTeacherMessage("system", data.body);
        showAlert(data.body, false);
        window.location.reload(); // pick up the new deadline cleanly
      } else if (data.kind === "submitted") {
        window.location = `/student/attempts/${ATTEMPT}/result`;
      }
    };
    src.onerror = () => { /* EventSource auto-reconnects */ };
  }

  function appendTeacherMessage(kind, body) {
    const box = $("#teacher-messages");
    if (!box) return;
    const div = document.createElement("div");
    div.className = "tmsg tmsg-" + kind;
    div.textContent = body;
    box.appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ------------------------------------------------------------------
  // Submit (only explicit student action or the timer hitting 0)
  // ------------------------------------------------------------------
  const btnSubmit = $("#btn-submit");

  async function submitExam(fromTimer) {
    if (state.submitted) return;
    if (!fromTimer) {
      const cards = $$(".question-card");
      let blank = 0;
      cards.forEach((card) => {
        if (!isAnswered(card.dataset.questionId, card)) blank++;
      });
      const msg = blank > 0
        ? `${blank} question(s) are unanswered. Submit anyway?`
        : "Submit the exam now?";
      // Use accessible modal instead of window.confirm
      const ok = await window.confirmAsync("Submit exam", msg);
      if (!ok) return;
    }
    state.submitted = true;
    if (btnSubmit) { btnSubmit.disabled = true; btnSubmit.textContent = "Submitting…"; }
    clearInterval(timerHandle);
    try {
      await post(`/api/attempts/${ATTEMPT}/submit`, { answers: mergedPayloadAnswers() });
      window.location = `/student/attempts/${ATTEMPT}/result`;
    } catch (err) {
      state.submitted = false;
      if (btnSubmit) { btnSubmit.disabled = false; btnSubmit.textContent = "✅ Submit exam"; }
      if (err.status === 409) {
        window.location = `/student/attempts/${ATTEMPT}/result`;
      } else {
        showAlert("Submit failed — check your connection and try again.", true);
      }
    }
  }

  if (btnSubmit) btnSubmit.addEventListener("click", () => submitExam(false));

  // Warn before closing/reloading mid-exam.
  window.addEventListener("beforeunload", (e) => {
    if (state.submitted) return;
    flushAnswers();
    e.preventDefault();
    e.returnValue = "";
  });

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  let timerHandle = null;

  function beginExam() {
    timerHandle = setInterval(tick, 500);
    tick();
    setInterval(heartbeat, 15000);
    setInterval(() => { if (!document.hidden) flushAnswers(); }, 20000);
    connectStream();
    if (payload.require_fullscreen && !document.fullscreenElement && fsOverlay) {
      fsOverlay.hidden = false;
    }
  }

  bindQuestions();
  bindNavigator();
  bindOneAtATime();

  // Webcam consent gate (if enabled), otherwise start immediately.
  if (payload.webcam_enabled && consentBox && !payload.consent) {
    // Wait for the student's choice; timer starts after they continue.
    if (timerHandle === null) timerHandle = setInterval(tick, 500);
    setInterval(heartbeat, 15000);
  } else {
    startWebcam();
    beginExam();
  }

  // Re-check the server deadline every 30s (protects against clock changes).
  setInterval(async () => {
    if (state.submitted) return;
    try {
      const res = await fetch(`/api/attempts/${ATTEMPT}/heartbeat`, {
        method: "POST",
        headers: csrfHeaders({ "Content-Type": "application/json" }),
        credentials: "same-origin",
        body: "{}",
      });
      if (res.status === 409 || res.status === 410) {
        window.location = `/student/attempts/${ATTEMPT}/result`;
      }
    } catch (e) { /* ignore */ }
  }, 30000);
})();
