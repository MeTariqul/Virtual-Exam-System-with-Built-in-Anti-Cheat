/* Shared question editor for the teacher pages.
 *
 * Three jobs:
 *   1. Every [data-qblock] gets its own "Question type" wiring, so the
 *      create-exam page can show many questions at once and each one
 *      shows only its own option/answer fields.
 *   2. The create-exam page adds new question blocks from
 *      <template id="q-template"> (cloned with a fresh q_<n>_ name prefix)
 *      and removes them again.
 *   3. The "from your question bank" list can be filtered as you type.
 *
 * Load it with <script defer> — it boots as soon as the DOM is ready.
 */
(function () {
  "use strict";

  /* ---------------- one block = one question ------------------------- */

  function applyType(block) {
    var select = block.querySelector("[data-qtype]");
    if (!select) return;
    var type = select.value;
    block.querySelectorAll("[data-for-type]").forEach(function (el) {
      el.hidden = el.dataset.forType.split(",").indexOf(type) === -1;
    });
  }

  function wireBlock(block) {
    if (block.dataset.qwired === "1") return;   // already live
    block.dataset.qwired = "1";
    var select = block.querySelector("[data-qtype]");
    if (!select) return;
    select.addEventListener("change", function () { applyType(block); });
    applyType(block);
  }

  function wireAllBlocks(root) {
    (root || document).querySelectorAll("[data-qblock]").forEach(wireBlock);
  }

  /* ---------------- add / remove inline blocks ----------------------- */

  function renumber(host) {
    host.querySelectorAll("[data-qblock]").forEach(function (block, index) {
      var num = block.querySelector("[data-qnum]");
      if (num) num.textContent = String(index + 1);
    });
  }

  /* Highest q_<n>_ index currently on the page, + 1.  Derived from the
     page itself (never a bare counter) so removing a block can never make
     two blocks share a name — names are how the server tells them apart. */
  function nextFreeIndex(host) {
    var max = -1;
    host.querySelectorAll("[data-qblock]").forEach(function (block) {
      var index = parseInt(block.dataset.qidx, 10);
      if (!isNaN(index) && index > max) max = index;
    });
    return max + 1;
  }

  function addBlock(host, template) {
    var index = nextFreeIndex(host);
    // The template is written with the q_0_ prefix; rewrite it to q_<n>_.
    var html = template.innerHTML.split("q_0_").join("q_" + index + "_");
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var block = holder.firstElementChild;
    if (!block) return;
    block.dataset.qidx = String(index);
    host.appendChild(block);
    wireBlock(block);
    renumber(host);
    var text = block.querySelector("textarea[name$='_text']");
    if (text) {
      text.focus();
      if (text.scrollIntoView) text.scrollIntoView({ block: "nearest" });
    }
  }

  function removeBlock(host, block) {
    var text = block.querySelector("textarea[name$='_text']");
    if (text && text.value.trim() &&
        !window.confirm("Remove this question? Its unsaved text will be lost.")) {
      return;
    }
    block.remove();
    renumber(host);
    // Nothing left? Bring one back so the teacher is never staring at
    // an empty form with no way in.
    if (!host.querySelector("[data-qblock]") && document.getElementById("q-add")) {
      addBlock(host, document.getElementById("q-template"));
    }
  }

  /* ---------------- bank filter -------------------------------------- */

  function wireBankFilter() {
    var input = document.getElementById("bank-filter");
    var list = document.getElementById("bank-list");
    if (!input || !list) return;
    var items = list.querySelectorAll(".bank-item[data-search]");
    var count = document.getElementById("bank-count");

    function update() {
      var needle = input.value.trim().toLowerCase();
      var shown = 0;
      items.forEach(function (item) {
        var hit = !needle || item.dataset.search.indexOf(needle) !== -1;
        item.style.display = hit ? "" : "none";
        if (hit) shown += 1;
      });
      if (count) {
        count.textContent = needle
          ? shown + " of " + items.length + " match"
          : items.length + " in your bank";
      }
    }

    input.addEventListener("input", update);
    update();
  }

  /* ---------------- boot --------------------------------------------- */

  function boot() {
    wireAllBlocks(document);

    var host = document.getElementById("q-blocks");
    var template = document.getElementById("q-template");
    var add = document.getElementById("q-add");

    if (host) {
      renumber(host);
      host.addEventListener("click", function (event) {
        var target = event.target;
        var button = target && target.closest
          ? target.closest("[data-remove-block]") : null;
        if (!button || !host.contains(button)) return;
        var block = button.closest("[data-qblock]");
        if (block) removeBlock(host, block);
      });
    }
    if (add && host && template) {
      add.addEventListener("click", function () { addBlock(host, template); });
    }

    wireBankFilter();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
