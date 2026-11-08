---
name: web-layout
roles: planner, tester, coder, reviewer
keywords: website, web site, page, landing, portfolio, section, layout, responsive, mobile, css, grid, homepage
---
Web layout:
- Semantic HTML: header with nav, main with one section per planned section (each with the planned id), footer.
- Mobile first: single column under 640 px, grid/flex columns above; nothing wider than the screen (no horizontal
  scroll). Test at 390 px and 1280 px wide.
- A content width of ~1100-1200 px centred; generous section padding (64-120 px on desktop, 48 px on phones).
- Type scale with clamp(): headings like clamp(2rem, 5vw, 4rem); line length 60-75 characters for paragraphs.
- Every image has alt text; links and buttons have visible focus styles; nav links point to section ids.
- CSS variables for the palette (--name from PALETTE) and spacing; no inline styles, no external fonts or CDNs.
