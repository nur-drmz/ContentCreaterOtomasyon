(() => {
  const cards = [...document.querySelectorAll(".moment-card")];
  const selectedList = document.querySelector("#selection-list");
  const selectionEmpty = document.querySelector("#selection-empty");
  const selectedCount = document.querySelector("#selected-count");
  const selectedFilterCount = document.querySelector("#selected-filter-count");
  const rejectedFilterCount = document.querySelector("#rejected-filter-count");
  const selectedDuration = document.querySelector("#selected-duration");
  const durationFill = document.querySelector("#duration-fill");
  const visibleCount = document.querySelector("#visible-count");
  const noResults = document.querySelector("#no-results");
  const scoreFilter = document.querySelector("#score-filter");
  const scoreOutput = document.querySelector("#score-output");
  const buildButtons = [
    document.querySelector("#build-cut"),
    document.querySelector("#build-cut-secondary"),
  ];

  function selectedCards() {
    return cards.filter((card) => card.dataset.decision === "selected");
  }

  function updateSelectionRail() {
    const selected = selectedCards();
    const rejected = cards.filter((card) => card.dataset.decision === "rejected");
    const duration = selected.reduce(
      (total, card) =>
        total + Number(card.querySelector(".visual-duration").textContent.split(" ")[0]),
      0,
    );

    selectedList.replaceChildren();
    selected.forEach((card, index) => {
      const title = card.querySelector("h2").textContent;
      const time = card.querySelector(".visual-time").textContent;

      const indexSpan = document.createElement("span");
      indexSpan.className = "selection-index";
      indexSpan.textContent = String(index + 1).padStart(2, "0");

      const info = document.createElement("div");
      const strong = document.createElement("strong");
      strong.textContent = title;
      const small = document.createElement("small");
      small.textContent = time;
      info.append(strong, small);

      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.dataset.remove = card.dataset.id;
      removeButton.setAttribute("aria-label", `Remove ${title}`);
      removeButton.textContent = "×";

      const item = document.createElement("li");
      item.append(indexSpan, info, removeButton);
      selectedList.append(item);
    });

    selectionEmpty.hidden = selected.length > 0;
    selectedCount.textContent = selected.length;
    selectedFilterCount.textContent = selected.length;
    rejectedFilterCount.textContent = rejected.length;
    selectedDuration.textContent = duration;
    durationFill.style.width = `${Math.min((duration / 45) * 100, 100)}%`;
    buildButtons.forEach((button) => {
      button.disabled = selected.length === 0;
    });
  }

  function applyFilters() {
    const decision = document.querySelector('input[name="decision"]:checked').value;
    const characters = [
      ...document.querySelectorAll('input[name="character"]:checked'),
    ].map((input) => input.value);
    const spoilers = [
      ...document.querySelectorAll('input[name="spoiler"]:checked'),
    ].map((input) => input.value);
    const minimumScore = Number(scoreFilter.value);
    let visible = 0;

    cards.forEach((card) => {
      const decisionMatch = decision === "all" || card.dataset.decision === decision;
      const characterMatch =
        characters.length === 0 || characters.includes(card.dataset.character);
      const spoilerMatch =
        spoilers.length === 0 || spoilers.includes(card.dataset.spoiler);
      const scoreMatch = Number(card.dataset.score) >= minimumScore;
      const show = decisionMatch && characterMatch && spoilerMatch && scoreMatch;
      card.hidden = !show;
      if (show) visible += 1;
    });

    visibleCount.textContent = visible;
    noResults.hidden = visible !== 0;
  }

  function setDecision(card, decision) {
    card.dataset.decision =
      card.dataset.decision === decision ? "undecided" : decision;
    card.classList.toggle("selected", card.dataset.decision === "selected");
    card.classList.toggle("rejected", card.dataset.decision === "rejected");
    updateSelectionRail();
    applyFilters();
  }

  function toggleInspect(card) {
    const panel = card.querySelector(".inspect-panel");
    if (!panel) return;
    const expanded = panel.hidden;
    panel.hidden = !expanded;
    card.querySelectorAll('[data-action="inspect"]').forEach((button) => {
      button.setAttribute("aria-expanded", String(expanded));
    });
  }

  cards.forEach((card) => {
    card.querySelectorAll("[data-action]").forEach((button) => {
      const action = button.dataset.action;
      button.addEventListener("click", () => {
        if (action === "inspect") {
          toggleInspect(card);
        } else {
          setDecision(card, action);
        }
      });
    });
  });

  selectedList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove]");
    if (!button) return;
    const card = cards.find((candidate) => candidate.dataset.id === button.dataset.remove);
    if (card) setDecision(card, "selected");
  });

  document.querySelectorAll(".filter-rail input").forEach((input) => {
    input.addEventListener("input", () => {
      scoreOutput.textContent = `${scoreFilter.value}+`;
      applyFilters();
    });
  });

  document.querySelector("#reset-filters").addEventListener("click", () => {
    document.querySelector('input[name="decision"][value="all"]').checked = true;
    document
      .querySelectorAll('input[name="character"], input[name="spoiler"]')
      .forEach((input) => {
        input.checked = false;
      });
    scoreFilter.value = 70;
    scoreOutput.textContent = "70+";
    applyFilters();
  });

  document.querySelector("#rerank-button").addEventListener("click", (event) => {
    const button = event.currentTarget;
    button.textContent = "Ranked ✓";
    window.setTimeout(() => {
      button.textContent = "Re-rank";
    }, 1400);
  });

  updateSelectionRail();
  applyFilters();
})();
