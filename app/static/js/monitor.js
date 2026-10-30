/* Teacher live monitor: SSE feed + quick actions (message / extend / end). */
(function () {
  "use strict";
  const cfg = window.MONITOR;
  if (!cfg) return;

  const feed = document.getElementById("violation-feed");
  const dot = document.getElementById("live-dot");
  const status = document.getElementById("live-status");

  const LABELS = {
    tab_switch: "Tab switch",
    window_blur: "Focus lost",
    fullscreen_exit: "Left fullscreen",
    copy_paste: "Copy/paste attempt",
    no_face: "No face",
    multiple_faces: "Multiple faces",
    monitor_gap: "Monitoring interrupted",
    short_focus_loss: "Brief focus loss (grace)",
    low_light: "Camera too dark",
    face_check_unavailable: "Face check unavailable",
  };

  function setLive(ok) {
    if (dot) {
      dot.classList.toggle("live-ok", ok);
      dot.classList.toggle("live-bad", !ok);
    }
    if (status) status.textContent = ok ? "live" : "reconnecting…";
  }

  // ---- SSE ----------------------------------------------------------
  if (window.EventSource) {
    const src = new EventSource(`/api/exams/${cfg.examId}/stream`);
    src.onopen = () => setLive(true);
    src.onerror = () => setLive(false);
    src.onmessage = (msg) => {
      let data;
      try { data = JSON.parse(msg.data); } catch (e) { return; }
      handleEvent(data);
    };
  } else {
    setLive(false);
  }

  function handleEvent(data) {
    if (data.kind === "violation") {
      addFeedItem(data);
      bumpRow(data.attempt_id, data.warning_count, data.over_limit);
    } else if (data.kind === "attempt_started" || data.kind === "attempt_submitted") {
      // Simplest reliable approach: refresh the attempt table.
      setTimeout(() => window.location.reload(), 1200);
    } else if (data.kind === "message") {
      addFeedItem({ label: "Teacher message sent", student: "—", time: data.time });
    }
  }

  function addFeedItem(data) {
    if (!feed) return;
    const empty = feed.querySelector(".muted");
    if (empty) empty.remove();
    const item = document.createElement("div");
    item.className = "feed-item" + (data.over_limit ? " feed-danger" : "") +
                     (data.ignored ? " feed-ignored" : "");
    const time = data.time ? new Date(data.time).toLocaleTimeString() : "";
    item.innerHTML =
      `<span class="feed-time">${time}</span>` +
      `<strong>${escapeHtml(data.student || "")}</strong>` +
      `<span class="feed-label">${escapeHtml(data.label || LABELS[data.type] || data.type || "")}</span>` +
      (data.warning_count !== undefined
        ? `<span class="badge ${data.over_limit ? "badge-risk-high" : "badge-risk-low"}">` +
          `${data.warning_count}/${cfg.limit}</span>` : "");
    feed.prepend(item);
    while (feed.children.length > 60) feed.removeChild(feed.lastChild);
  }

  function bumpRow(attemptId, count, over) {
    const row = document.querySelector(`tr[data-attempt-id="${attemptId}"]`);
    if (!row) return;
    const cell = row.querySelector(".warn-cell .badge");
    if (cell && count !== undefined) {
      cell.textContent = `${count}/${cfg.limit}`;
      cell.classList.toggle("badge-risk-high", !!over);
      cell.classList.toggle("badge-risk-low", !over);
    }
    row.classList.remove("flash-row");
    void row.offsetWidth; // restart animation
    row.classList.add("flash-row");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---- Countdown timers --------------------------------------------
  setInterval(() => {
    document.querySelectorAll(".time-cell[data-deadline]").forEach((cell) => {
      const row = cell.closest("tr");
      if (row && row.querySelector(".badge-in_progress") === null) return;
      const left = Math.max(0, Math.floor((new Date(cell.dataset.deadline) - Date.now()) / 1000));
      const m = Math.floor(left / 60), s = left % 60;
      cell.textContent = `${m}:${String(s).padStart(2, "0")}`;
      cell.classList.toggle("time-danger", left > 0 && left <= 60);
    });
  }, 1000);

  // ---- Quick actions ------------------------------------------------
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const attempt = btn.dataset.attempt;
      const action = btn.dataset.action;
      let url = `/teacher/attempts/${attempt}/${action === "message" ? "message" : action === "extend" ? "extend" : "force-submit"}`;
      let body = null;

      if (action === "message") {
        const text = prompt("Message to this student:");
        if (!text) return;
        body = JSON.stringify({ body: text });
      } else if (action === "extend") {
        if (!confirm("Extend this attempt by 5 minutes?")) return;
        body = JSON.stringify({ minutes: 5 });
      } else {
        if (!confirm("End this attempt now and grade it? The student will see their result.")) return;
        body = JSON.stringify({});
      }

      const res = await fetch(url, {
        method: "POST",
        headers: {
          "X-CSRFToken": cfg.csrfToken,
          "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: body,
      });
      if (res.ok) {
        if (action !== "message") window.location.reload();
      } else {
        const data = await res.json().catch(() => ({}));
        alert(data.error || "Action failed.");
      }
    });
  });
})();
