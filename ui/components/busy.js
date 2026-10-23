// A control that is working, or can't be used just now, without leaving the keyboard's place.
//
// A button set `disabled` while the user is on it loses keyboard focus to the page (in every browser),
// so a keyboard or screen-reader user has to find their way back after each press. aria-disabled="true"
// says the same to assistive technology and to base.css (dimmed, no pointer), but the control stays
// focusable. It is also still activatable, so whoever handles its clicks must ignore them while it is
// inert: busy() does, and a handler of its own checks isInert().
//
//   onClick: () => busy(button, () => api.doIt())    // inert while the promise runs; ignored when already inert
//   setInert(button, state.busy)                    // for state a component keeps itself
//   if (isInert(button)) return;                    // first line of such a control's handler
//
// Native `disabled` is for a control that is not applicable at all (and should be skipped by Tab).

/** Whether control is aria-disabled. */
export function isInert(control) {
  return control.getAttribute("aria-disabled") === "true";
}

/** Make control inert (or active again) without moving focus. */
export function setInert(control, inert) {
  if (inert) control.setAttribute("aria-disabled", "true");
  else control.removeAttribute("aria-disabled");
}

/**
 * Run work() with control inert, then make it active again (even if work throws; the error goes on to
 * the caller). Does nothing, returning undefined, when control is inert already: a second click while
 * the first is working, or a control that isn't available.
 */
export async function busy(control, work) {
  if (isInert(control)) return undefined;
  setInert(control, true);
  try {
    return await work();
  } finally {
    setInert(control, false);
  }
}

/** A switch (checkbox) that is inert shouldn't toggle: cancel the click that would. */
export function holdWhileInert(control) {
  control.addEventListener("click", (event) => {
    if (isInert(control)) event.preventDefault();
  });
}
