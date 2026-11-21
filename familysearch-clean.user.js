// ==UserScript==
// @name         FamilySearch - Clean archive viewer
// @namespace    https://github.com/AntonPasiy/familysearch-image-downloader
// @version      0.1.0
// @description  Temporarily hide viewer HUD and page headers for unobstructed viewing. Toggle Ctrl+Alt+H.
// @match        https://www.familysearch.org/*
// @grant        none
// @run-at       document-end
// ==/UserScript==

(() => {
    'use strict';

    const STATE_ATTRIBUTE = 'data-fsd-clean-view';
    const STYLE_ID = 'fsd-clean-view-style';
    const STYLE = `
        /* 1. Image viewer HUD: frame number, zoom, adjustment, download, etc. */
        html[${STATE_ATTRIBUTE}="on"] [data-left-controls] {
            visibility: hidden !important;
            pointer-events: none !important;
        }

        /* 2. Archive title, breadcrumbs, save-record and information bar. */
        html[${STATE_ATTRIBUTE}="on"] div[class*="marginBoxCss_"]:has([data-testid="header-back-button"]) {
            display: none !important;
        }

        /* 3. Site-wide navigation header (the FamilySearch logo and menus). */
        html[${STATE_ATTRIBUTE}="on"] div[class*="headerContainerCss_"]:has(header a[href*="/home/portal/"]) {
            display: none !important;
        }
    `;

    const root = document.documentElement;
    if (!root || document.getElementById(STYLE_ID)) return;
    const stylesheet = document.createElement('style');
    stylesheet.id = STYLE_ID;
    stylesheet.textContent = STYLE;
    (document.head || root).appendChild(stylesheet);

    // No auto-hiding: set zoom and the starting frame before hiding controls.
    // CSS reacts to later React component mounts without a MutationObserver.
    document.addEventListener('keydown', (event) => {
        if (
            event.ctrlKey && event.altKey && !event.shiftKey && !event.metaKey &&
            event.key.toLowerCase() === 'h' &&
            !event.repeat
        ) {
            const target = event.target;
            if (
                target instanceof HTMLElement &&
                (target.isContentEditable || target.closest('input, textarea, select'))
            ) return;
            event.preventDefault();
            const enabled = root.getAttribute(STATE_ATTRIBUTE) === 'on';
            root.setAttribute(STATE_ATTRIBUTE, enabled ? 'off' : 'on');
            console.info(`[fsd] Clean viewer ${enabled ? 'disabled' : 'enabled'} (Ctrl+Alt+H)`);
        }
    });
})();
