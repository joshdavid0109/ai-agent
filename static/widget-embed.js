/**
 * AI Chat Widget — Embed Snippet
 *
 * Drop this script tag into any website to add the AI chat widget:
 *
 *   <script src="https://YOUR_DOMAIN/static/widget-embed.js"
 *           data-widget-url="https://YOUR_DOMAIN/widget"></script>
 *
 * Configuration (via data attributes):
 *   data-widget-url    — URL of the widget (required)
 *   data-position      — "right" (default) or "left"
 *   data-bubble-color  — CSS color for the bubble (default: gradient)
 *   data-width         — Widget width in px (default: 380)
 *   data-height        — Widget height in px (default: 560)
 */

(function () {
    'use strict';

    // Read config from script tag
    const script = document.currentScript;
    const widgetUrl = script.getAttribute('data-widget-url') || '/widget';
    const position = script.getAttribute('data-position') || 'right';
    const width = parseInt(script.getAttribute('data-width')) || 380;
    const height = parseInt(script.getAttribute('data-height')) || 560;
    const bubbleColor = script.getAttribute('data-bubble-color') || '';

    // Inject styles
    const style = document.createElement('style');
    style.textContent = `
        #ai-chat-widget-bubble {
            position: fixed;
            bottom: 24px;
            ${position}: 24px;
            width: 56px;
            height: 56px;
            border-radius: 50%;
            background: ${bubbleColor || 'linear-gradient(135deg, #10a37f, #06b6d4)'};
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            cursor: pointer;
            box-shadow: 0 4px 20px rgba(16, 163, 127, 0.4);
            transition: all 0.3s ease;
            z-index: 99999;
            border: none;
            font-family: sans-serif;
        }
        #ai-chat-widget-bubble:hover {
            transform: scale(1.1);
            box-shadow: 0 6px 28px rgba(16, 163, 127, 0.5);
        }
        #ai-chat-widget-bubble.open {
            transform: rotate(90deg);
        }
        #ai-chat-widget-frame {
            position: fixed;
            bottom: 92px;
            ${position}: 24px;
            width: ${width}px;
            height: ${height}px;
            border: none;
            border-radius: 16px;
            box-shadow: 0 8px 40px rgba(0, 0, 0, 0.15);
            z-index: 99998;
            overflow: hidden;
            display: none;
            opacity: 0;
            transform: translateY(16px) scale(0.96);
            transition: opacity 0.3s ease, transform 0.3s ease;
        }
        #ai-chat-widget-frame.open {
            display: block;
            opacity: 1;
            transform: translateY(0) scale(1);
        }
        @media (max-width: 480px) {
            #ai-chat-widget-frame {
                width: calc(100vw - 16px);
                height: calc(100vh - 120px);
                bottom: 80px;
                ${position}: 8px;
                border-radius: 12px;
            }
            #ai-chat-widget-bubble {
                bottom: 16px;
                ${position}: 16px;
            }
        }
    `;
    document.head.appendChild(style);

    // Create bubble
    const bubble = document.createElement('button');
    bubble.id = 'ai-chat-widget-bubble';
    bubble.textContent = '💬';
    document.body.appendChild(bubble);

    // Create iframe
    const iframe = document.createElement('iframe');
    iframe.id = 'ai-chat-widget-frame';
    iframe.src = widgetUrl;
    iframe.title = 'AI Chat Widget';
    iframe.allow = 'clipboard-write';
    document.body.appendChild(iframe);

    // Toggle logic
    let isOpen = false;
    bubble.addEventListener('click', function () {
        isOpen = !isOpen;
        iframe.classList.toggle('open', isOpen);
        bubble.classList.toggle('open', isOpen);
        bubble.textContent = isOpen ? '✕' : '💬';

        if (isOpen) {
            iframe.contentWindow.postMessage(
                { source: 'ai-chat-host', type: 'open' }, '*'
            );
        }
    });

    // Listen for widget messages
    window.addEventListener('message', function (event) {
        const data = event.data;
        if (!data || data.source !== 'ai-chat-widget') return;

        if (data.type === 'widget_ready') {
            console.log('[AI Widget] Ready');
        }
    });
})();
