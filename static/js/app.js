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
  const header = document.querySelector(".app-header");
  const menu = document.querySelector("#menu");
  const toggle = document.querySelector("[data-menu-toggle='main']");

  if (!menu || !toggle) return;

  const syncHeaderHeight = () => {
    const height = header ? header.offsetHeight : 78;
    document.documentElement.style.setProperty("--app-header-height", `${height}px`);
  };

  const setOpenState = (isOpen) => {
    syncHeaderHeight();
    toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    menu.classList.toggle("is-open", isOpen);
    menu.setAttribute("aria-hidden", isOpen ? "false" : "true");
    document.body.classList.toggle("menu-open", isOpen);
    document.body.classList.remove("offcanvas-open");
    document.querySelectorAll(".app-menu-backdrop, .offcanvas-backdrop").forEach((el) => el.remove());
  };

  const closeMenu = () => setOpenState(false);

  toggle.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setOpenState(!menu.classList.contains("is-open"));
  });

  menu.querySelectorAll("[data-menu-close], .nav-link, a.btn").forEach((item) => {
    item.addEventListener("click", closeMenu);
  });

  document.addEventListener("click", (event) => {
    if (!menu.classList.contains("is-open")) return;
    if (menu.contains(event.target) || toggle.contains(event.target)) return;
    closeMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.classList.contains("is-open")) {
      closeMenu();
    }
  });

  window.addEventListener("resize", syncHeaderHeight);
  syncHeaderHeight();
})();
