document.querySelectorAll(".alert").forEach((el) => {
  setTimeout(() => el.remove(), 7000);
});

(() => {
  const backButtons = document.querySelectorAll("[data-history-back]");

  backButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (window.history.length > 1) {
        window.history.back();
      } else {
        window.location.href = "/";
      }
    });
  });
})();

(() => {
  // Mission 7A : le menu principal utilise désormais le composant Bootstrap
  // natif "collapse" (navbar-expand-lg), géré par bootstrap.bundle.min.js —
  // plus besoin de script maison pour l'ouvrir/fermer. On referme juste le
  // menu mobile après un clic sur un lien, comme une navbar Bootstrap standard.
  const collapseEl = document.querySelector(".app-navbar .navbar-collapse");
  if (!collapseEl || !window.bootstrap) return;
  collapseEl.querySelectorAll(".nav-link:not(.dropdown-toggle), .dropdown-item").forEach((link) => {
    link.addEventListener("click", () => {
      if (!collapseEl.classList.contains("show")) return;
      bootstrap.Collapse.getOrCreateInstance(collapseEl).hide();
    });
  });
})();
