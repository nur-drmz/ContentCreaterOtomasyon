(() => {
  const cards = [...document.querySelectorAll(".moment-card")];
  const selectedCountEl = document.getElementById("pm-selected-count");
  const rejectedCountEl = document.getElementById("pm-rejected-count");
  const selectedDurationEl = document.getElementById("pm-selected-duration");
  const targetDurationEl = document.getElementById("pm-target-duration");
  const durationFillEl = document.getElementById("pm-duration-fill");

  function toggleInspect(card) {
    const button = card.querySelector('[data-action="inspect"]');
    const panel = card.querySelector(".inspect-panel");
    if (!panel || !button) return;
    const expanded = panel.hidden;
    panel.hidden = !expanded;
    button.setAttribute("aria-expanded", String(expanded));
  }

  cards.forEach((card) => {
    const inspectButton = card.querySelector('[data-action="inspect"]');
    if (inspectButton) {
      inspectButton.addEventListener("click", () => toggleInspect(card));
    }
  });

  function setCardDecision(card, decision) {
    card.classList.toggle("selected", decision === "SELECTED");
    card.classList.toggle("rejected", decision === "REJECTED");
    const restoreForm = card.querySelector(".restore-form");
    if (restoreForm) {
      restoreForm.hidden = decision === "UNDECIDED";
    }
  }

  // Every value here comes straight from the server's authoritative,
  // database-derived summary payload — nothing is computed in the browser.
  function applySummary(summary) {
    if (selectedCountEl) selectedCountEl.textContent = summary.selected_count;
    if (rejectedCountEl) rejectedCountEl.textContent = summary.rejected_count;
    if (selectedDurationEl) selectedDurationEl.textContent = summary.selected_duration;
    if (targetDurationEl) targetDurationEl.textContent = summary.target_duration;
    if (durationFillEl) durationFillEl.style.width = `${summary.duration_percent}%`;
  }

  function setCardControlsDisabled(card, disabled) {
    card.querySelectorAll(".decision-form button").forEach((button) => {
      button.disabled = disabled;
    });
  }

  document.querySelectorAll(".decision-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const card = form.closest(".moment-card");
      if (!card) return;

      // Guard against a second submit firing before the first request (and
      // the disabling of the buttons below) has taken effect.
      if (form.dataset.submitting === "true") return;
      form.dataset.submitting = "true";
      setCardControlsDisabled(card, true);

      fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        body: new FormData(form),
      })
        .then((response) => {
          if (!response.ok) throw new Error(`Request failed: ${response.status}`);
          return response.json();
        })
        .then((data) => {
          setCardDecision(card, data.decision);
          applySummary(data);
        })
        .catch(() => {
          // JavaScript or the fetch enhancement failed — fall back to a
          // normal form submission so the decision still persists via a
          // full page reload.
          HTMLFormElement.prototype.submit.call(form);
        })
        .finally(() => {
          form.dataset.submitting = "false";
          setCardControlsDisabled(card, false);
        });
    });
  });
})();
