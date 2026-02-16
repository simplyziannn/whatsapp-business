(function () {
  const nav = document.querySelector('.center-nav');
  if (!nav) return;

  const highlight = nav.querySelector('.nav-highlight');
  const items = nav.querySelectorAll('.nav-link');
  if (!highlight || items.length === 0) return;

  function positionHighlight(el) {
    if (!el) return;
    const navRect = nav.getBoundingClientRect();
    const elRect = el.getBoundingClientRect();
    const x = elRect.left - navRect.left;

    highlight.style.width = `${elRect.width}px`;
    highlight.style.transform = `translateX(${x}px)`;
  }

  items.forEach((item) => {
    item.addEventListener('mouseenter', () => {
      nav.classList.add('is-hovering');
      positionHighlight(item);
    });

    item.addEventListener('focus', () => {
      nav.classList.add('is-hovering');
      positionHighlight(item);
    });
  });

  nav.addEventListener('mouseleave', () => {
    nav.classList.remove('is-hovering');
  });

  window.addEventListener('resize', () => {
    const hovered = nav.querySelector(':hover');
    if (hovered && hovered.classList && hovered.classList.contains('nav-link')) {
      positionHighlight(hovered);
    }
  });

  requestAnimationFrame(() => {
    document.body.classList.add('is-ready');
  });
})();
