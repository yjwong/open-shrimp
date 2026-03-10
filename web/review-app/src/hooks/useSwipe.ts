import { useRef, useCallback } from "react";
import { useDrag } from "@use-gesture/react";

export type SwipeDirection = "left" | "right" | "down" | null;

interface SpringState {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface UseSwipeOptions {
  onSwipe: (direction: SwipeDirection) => void;
  enabled?: boolean;
}

interface UseSwipeResult {
  bind: ReturnType<typeof useDrag>;
  cardRef: React.RefObject<HTMLDivElement | null>;
  isAnimating: boolean;
}

const SWIPE_X_THRESHOLD = 0.3; // 30% of viewport width
const SWIPE_Y_THRESHOLD = 0.3; // 30% of viewport height
const VELOCITY_THRESHOLD = 0.5;
const AXIS_LOCK_PX = 10;

const SPRING_STIFFNESS = 0.15;
const SPRING_DAMPING = 0.7;

const EXIT_DISTANCE = 1.5; // multiplier of viewport dimension

function animateSpring(
  el: HTMLElement,
  initial: SpringState,
  target: { x: number; y: number },
  onComplete: () => void,
) {
  const state = { ...initial };
  let raf: number;
  const dt = 1;

  function step() {
    const dx = state.x - target.x;
    const dy = state.y - target.y;

    state.vx += -SPRING_STIFFNESS * dx - SPRING_DAMPING * state.vx;
    state.vy += -SPRING_STIFFNESS * dy - SPRING_DAMPING * state.vy;
    state.x += state.vx * dt;
    state.y += state.vy * dt;

    const rotation = state.x * 0.05;
    el.style.transform = `translate3d(${state.x}px, ${state.y}px, 0) rotate(${rotation}deg)`;

    const distToTarget = Math.sqrt(
      (state.x - target.x) ** 2 + (state.y - target.y) ** 2,
    );
    const speed = Math.sqrt(state.vx ** 2 + state.vy ** 2);

    if (distToTarget < 0.5 && speed < 0.5) {
      el.style.transform =
        target.x === 0 && target.y === 0
          ? "translate3d(0, 0, 0)"
          : `translate3d(${target.x}px, ${target.y}px, 0)`;
      onComplete();
      return;
    }

    raf = requestAnimationFrame(step);
  }

  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

export function useSwipe({ onSwipe, enabled = true }: UseSwipeOptions): UseSwipeResult {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const isAnimatingRef = useRef(false);
  const lockedAxisRef = useRef<"x" | "y" | null>(null);
  const cancelAnimRef = useRef<(() => void) | null>(null);

  const bind = useDrag(
    ({ down, movement: [mx, my], velocity: [vx, vy], cancel, first }) => {
      if (!enabled || isAnimatingRef.current) {
        cancel();
        return;
      }

      const el = cardRef.current;
      if (!el) return;

      if (first) {
        lockedAxisRef.current = null;
      }

      // Axis locking
      if (lockedAxisRef.current === null) {
        if (Math.abs(mx) > AXIS_LOCK_PX || Math.abs(my) > AXIS_LOCK_PX) {
          lockedAxisRef.current = Math.abs(mx) > Math.abs(my) ? "x" : "y";
        }
      }

      const effectiveMx = lockedAxisRef.current === "y" ? 0 : mx;
      const effectiveMy = lockedAxisRef.current === "x" ? 0 : my;
      // Only allow downward movement
      const clampedMy = effectiveMy > 0 ? effectiveMy : 0;

      if (down) {
        const rotation = effectiveMx * 0.05;
        el.style.transform = `translate3d(${effectiveMx}px, ${clampedMy}px, 0) rotate(${rotation}deg)`;
        return;
      }

      // Release — determine if swipe threshold met
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      let direction: SwipeDirection = null;

      if (lockedAxisRef.current === "x" || lockedAxisRef.current === null) {
        if (effectiveMx > vw * SWIPE_X_THRESHOLD || vx > VELOCITY_THRESHOLD) {
          direction = "right";
        } else if (effectiveMx < -vw * SWIPE_X_THRESHOLD || vx > VELOCITY_THRESHOLD && effectiveMx < 0) {
          direction = "left";
        }
      }

      if (
        direction === null &&
        (lockedAxisRef.current === "y" || lockedAxisRef.current === null)
      ) {
        if (clampedMy > vh * SWIPE_Y_THRESHOLD || vy > VELOCITY_THRESHOLD) {
          direction = "down";
        }
      }

      isAnimatingRef.current = true;

      if (direction === null) {
        // Spring back to center
        cancelAnimRef.current = animateSpring(
          el,
          { x: effectiveMx, y: clampedMy, vx: 0, vy: 0 },
          { x: 0, y: 0 },
          () => {
            isAnimatingRef.current = false;
          },
        );
      } else {
        // Animate off-screen
        const target = {
          x:
            direction === "right"
              ? vw * EXIT_DISTANCE
              : direction === "left"
                ? -vw * EXIT_DISTANCE
                : 0,
          y: direction === "down" ? vh * EXIT_DISTANCE : 0,
        };

        cancelAnimRef.current = animateSpring(
          el,
          { x: effectiveMx, y: clampedMy, vx: direction === "down" ? 0 : vx * 100, vy: direction === "down" ? vy * 100 : 0 },
          target,
          () => {
            isAnimatingRef.current = false;
            onSwipe(direction);

            // Reset position for next card
            if (el) {
              el.style.transform = "translate3d(0, 0, 0)";
            }
          },
        );
      }
    },
    {
      filterTaps: true,
      pointer: { touch: true },
    },
  );

  const cleanup = useCallback(() => {
    if (cancelAnimRef.current) {
      cancelAnimRef.current();
      cancelAnimRef.current = null;
    }
  }, []);

  // Cleanup is available but not auto-called — caller manages lifecycle
  void cleanup;

  return {
    bind,
    cardRef,
    isAnimating: isAnimatingRef.current,
  };
}
