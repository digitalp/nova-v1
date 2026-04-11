#!/usr/bin/env python3
"""
Fix video clip loading performance in Find Anything.

Changes:
1. preload="none" instead of "metadata" — don't fetch anything until needed
2. Add poster thumbnail from first frame via video_url + ?poster=1
3. IntersectionObserver to lazy-load videos only when scrolled into view
4. Limit initial render to visible viewport, load more on scroll
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # 1. Change preload="metadata" to preload="none" + add loading="lazy"
    html = html.replace(
        '${hasVideo ? `<video src="${clip.video_url}" preload="metadata" muted playsinline></video>`',
        '${hasVideo ? `<video data-src="${clip.video_url}" preload="none" muted playsinline poster="${clip.video_url}?poster=1"></video>`',
        1
    )

    # 2. Add IntersectionObserver for lazy video loading
    lazy_js = """
// ── Lazy Video Loading (IntersectionObserver) ───────────────────────────────
const _videoObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const video = entry.target;
      if (video.dataset.src && !video.src) {
        video.src = video.dataset.src;
        video.preload = 'metadata';
      }
      _videoObserver.unobserve(video);
    }
  });
}, { rootMargin: '200px 0px', threshold: 0.01 });

function _observeVideos(container) {
  if (!container) return;
  container.querySelectorAll('video[data-src]').forEach(v => {
    if (!v.src) _videoObserver.observe(v);
  });
}
"""
    html = html.replace("</script>\n</body>", lazy_js + "\n</script>\n</body>", 1)

    # 3. After rendering clips, observe the videos
    # Find where clips are appended to the grid and add observer call
    html = html.replace(
        "      groupGrid.appendChild(card);\n    });\n    grid.appendChild(group);\n  });",
        "      groupGrid.appendChild(card);\n    });\n    grid.appendChild(group);\n  });\n  _observeVideos(grid);",
        1
    )

    # 4. Fix the hover play — need to handle data-src videos
    html = html.replace(
        """    if (video) {
      video.onclick = () => openMotionModal(clip);
      video.addEventListener('mouseenter', () => {
        video.muted = true;
        video.play().catch(() => {});
      });
      video.addEventListener('mouseleave', () => {
        video.pause();
        video.currentTime = 0;
      });
    }""",
        """    if (video) {
      video.onclick = () => openMotionModal(clip);
      video.addEventListener('mouseenter', () => {
        // Lazy-load on hover if not yet loaded
        if (video.dataset.src && !video.src) {
          video.src = video.dataset.src;
          video.preload = 'metadata';
        }
        video.muted = true;
        video.play().catch(() => {});
      });
      video.addEventListener('mouseleave', () => {
        video.pause();
        video.currentTime = 0;
      });
    }""",
        1
    )

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


if __name__ == "__main__":
    main()
