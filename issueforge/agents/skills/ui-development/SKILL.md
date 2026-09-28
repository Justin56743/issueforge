---
name: ui-development
description: Production-grade UI component engineering, semantic HTML5, accessible Tailwind CSS, reactive DOM updates, xterm.js terminal integration, and keyboard shortcuts.
---

# UI Development: Production Component Engineering

Engineering robust, accessible, and performant web interfaces and components for real-time developer tooling and dashboards.

## Core Directives

1. **Semantic HTML5 & Accessible Structure**:
   - Use proper semantic elements (`<header>`, `<main>`, `<aside>`, `<section>`, `<nav>`).
   - Include appropriate ARIA attributes, labels, and title hints for accessibility and screen readers.

2. **Tailwind CSS Best Practices**:
   - Write clean utility classes avoiding redundant overrides.
   - Design for responsiveness (`sm:`, `md:`, `lg:` breakpoints).
   - Ensure high contrast in dark mode layouts and readable text across varying screen resolutions.

3. **Client-Side Interactivity & State**:
   - Keep vanilla JavaScript modular, decoupled, and error-tolerant.
   - Use `DOMContentLoaded` guards, graceful null checks (`if (element)`), and try/catch blocks around asynchronous `fetch` calls.
   - Implement snappy keyboard shortcuts (e.g. `Ctrl+S` / `Cmd+S` for file saving, `Enter` for submitting directives) with `e.preventDefault()`.

4. **Terminal & Stream Component Handling**:
   - Seamlessly mount and configure `xterm.js` terminals with `FitAddon` and proper resize handlers.
   - Handle WebSocket and Server-Sent Event (SSE) lifecycles cleanly, including automatic reconnection and graceful connection cleanup on page unmount.
