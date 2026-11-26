import { useLayoutEffect, useRef, useState } from "react";

/** An element's width, kept current as it resizes: for layouts that depend on the room a
 *  component actually has, not on the viewport (the sidebar may be wide, narrow or hidden). */
export function useWidth<T extends HTMLElement = HTMLDivElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}
