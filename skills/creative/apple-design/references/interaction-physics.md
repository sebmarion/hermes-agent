# Interaction Physics

> These values are starting points from the pinned source, not product
> requirements. Existing behavior and measured evidence win.

## Immediate and Continuous Response

Retain timestamped input samples for velocity and history, and update the
latest interaction state as samples arrive. Render that state at the highest
frame rate the target device can sustain, coalescing samples into the next
available animation frame when input arrives faster than display refresh. The
user's expectation is that motion begins without perceptible delay and every
rendered frame reflects the newest state. Avoid JavaScript main-thread stalls,
forced synchronous layout, and compositor blocks on non-atomic properties.
Prefer `requestAnimationFrame`-driven loops, compositor-friendly properties
(`transform`, `opacity`), and off-main-thread compositing wherever the project
stack supports it.

If the target constraints cannot sustain smooth motion, a reduced or direct-state
mechanism at the current position is appropriate. For motion loops that the stack
does support, prefer `requestAnimationFrame`-driven loops.

## One-to-One Direct Manipulation

The user's pointer, finger, or trackpad is the anchor; the draggable element is
the visual payload. Maintain a grab offset so the element does not "snap" to a
different corner on the first move, and keep that offset constant across the
entire gesture. Record a short, timestamped position history so the next
sample can compute a real gesture velocity instead of guessing from the first
frame's delta.

The interaction uses the Pointer Events API. When the user presses a
draggable surface, take `setPointerCapture` so the element continues tracking
the user's pointer even if the pointer drifts off the element. Release the
capture exactly once, at the end of the gesture.

## Interruptibility and Presentation-Value Continuity

A gesture that has started is a thing in progress, not a thing in transit. When
the user interrupts mid-gesture — by pressing again, by issuing a new
command, by changing their mind — the next sample must read from the live
presentation value, never the stale target. Never restart a spring from a
target position that is no longer valid. If the user is dragging and the
system is simultaneously animating a non-essential visual, the drag wins and
the animation yields its current transform.

Keep X and Y motion independently retargetable so that interrupting one axis
does not re-initiate motion on the other.

## Springs as Behavior

A spring is appropriate when direct manipulation or genuinely interruptible
physical behavior should carry weight, slow itself down, and arrive at the
right place. It is not the default for every clickable or touchable control.
For qualifying behavior, the deterministic starting point is a critically
damped spring with a damping ratio of `1.0` (no overshoot), with a response time
in the range `0.3–0.4` seconds as the speed starting point. Reserve a damping
ratio of approximately `0.8` for momentum-driven interactions such as flicks,
throws, or drag releases, where a little overshoot and settle-back reinforces
the sense of weight.

Springs are behavioral, not decorative. Use one only when the interaction's
physical continuity benefits from it; ordinary control feedback should keep
the product's existing transition or no-motion behavior.

## Velocity Sampling and Handoff

Absolute velocity, not relative delta, is the value the user is asking about.
Report it in screen pixels per second at the moment of release, after the last
sample, before any momentum projection runs.

For APIs expecting absolute velocity, pass the sampled px/s value directly.
For APIs expecting relative velocity, divide by the remaining distance, with
the guarded formula:

```js
const distance = targetValue - currentValue;
const relativeVelocity = distance === 0 ? 0 : gestureVelocity / distance;
```

## Momentum Projection and Snap Points

Momentum is the visible extension of velocity past the end of user input. It
is not a separate animation; it is the continuation of the same spring. The
endpoint a flick would reach if the spring kept running, at the project's
current deceleration rate, is computed from:

```js
function project(initialVelocity, decelerationRate = 0.998) {
  return (initialVelocity / 1000) *
    decelerationRate / (1 - decelerationRate);
}

const projectedEndpoint = currentPosition + project(releaseVelocity);
const target = nearestSnapPoint(projectedEndpoint);
```

Snap points are user-facing destinations the user is asking for. Pick the
nearest one to the projected endpoint, and let the spring pull the element
there from the current position. If there are no snap points, let the spring
run out at the projected endpoint.

## Spatial Consistency and Reversible Paths

Every physical path in the product — a swipe that opens a sheet, a flick that
throws a carousel, a drag that moves a card — must look the same the next time
the user does it. Content exits along the same path it entered. Menus,
popovers, and sheets originate from their triggering element (source-anchored
transform origin).

When a gesture reverses (the user starts a drag in one direction, then reverses
direction), the path must reverse too, smoothly. If the project's existing
stack cannot render a smooth reversal at the project's target frame rate,
fall back to a reduced or static transition at the current position.

## Intermediate Motion Signals the Destination

As the user drags, the element's position tells them where it will land. If
the element lands at the edge of a scrollable container, show the container's
edge behavior — a rubber band, a shadow, a fade — at the same scale and
timing the user will see at the end of the flick. If the element lands at a
snap point, land there. If the element lands past a snap point, snap to the
nearest one on the far side.

## Rubber-Banding at Boundaries

Rubber-band behavior (sometimes called "rubberbanding") is the system's way of
saying "you are at the edge; there is nothing further to pull." It is not a
separate animation; it is the spring at the edge, with a nonlinear
relationship:

```js
function rubberband(overshoot, dimension, constant = 0.55) {
  return (overshoot * dimension * constant) /
    (dimension + constant * Math.abs(overshoot));
}
```

Rubber-band resistance begins continuously at the boundary; it is not gated
behind a dead-zone threshold. The nonlinear curve progressively reduces travel
as the overshoot grows.

## Gesture Disambiguation and Cancellation

Pointer-down may begin immediate visual feedback but does not activate. Native
click/touch-up commits pointer activation; keyboard and assistive technology
activation pathways remain intact. Cancellation suppresses activation or
restores state.

A tap remains a candidate while movement stays within a reversible
tap-cancellation boundary, with approximately `10px` of gesture hysteresis as a
starting point. Dragging away can cancel a pending tap, and returning below the
threshold before release can restore it while the gesture remains uncommitted.
Treat drag recognition as a separate state transition with its own
drag-recognition threshold based on movement, direction, and competing scroll
intent; do not use the reversible tap boundary alone as drag commitment. Once
movement crosses the drag-recognition threshold, commit to a drag direction and
track 1:1; after drag commitment, returning below the threshold does not restore
tap candidacy. Detect plausible gestures in parallel, then cancel the losing
interpretations once intent is clear.

When the user cancels a gesture — by pressing Escape, by issuing a new
command, by navigating away — the element must return to its pre-gesture
position, smoothly, at the speed the user expects. If the project's existing
stack cannot render a smooth reversal, use a reduced or static transition at
the pre-gesture position.

## Frame-Level Smoothness

Motion must be smooth at the frame level. Retain relevant input samples for
state and velocity history, but make at most one visual mutation per animation
frame and render the latest state. Advance each spring step once per frame. Use
compositor-friendly properties (`transform`, `opacity`) for motion. Avoid
non-compositor-friendly properties (`left`, `top`, `width`, `height`). If the
target constraints cannot meet smooth motion, fall back to a reduced or static
mechanism at the current position.

`requestAnimationFrame` must be used for motion loops. If the stack cannot
support `requestAnimationFrame`, fall back to a direct-state mechanism at the
current position; never use `setTimeout` or `setInterval` for motion loops.

## Starting Points, Not Requirements

The values above are starting points, not product requirements. Existing
behavior and measured evidence win. If the project's existing behavior does
not match these values, do not change it to match these values. If the project
does not currently support these values, do not add them unless the user has
explicitly asked for them.

If the project already uses Motion or Framer Motion, these values can be used
as a mapping layer on top of the project's existing motion system. Otherwise,
do not add Motion or Framer Motion.

## Framework and Dependency Boundary

This skill does not prescribe a framework, a motion library, a dependency, or
a runtime. It prescribes behavior. If the project's existing stack already
implements this behavior, use it. If the project's existing stack does not
implement this behavior, implement it using the project's existing stack.
