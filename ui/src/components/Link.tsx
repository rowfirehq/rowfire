import type { AnchorHTMLAttributes, ReactNode } from "react";

import { follow } from "../router";

/* A real <a href>, so a link can be opened in a new tab, copied or
 * bookmarked, that navigates in-page on a plain click. */
export function Link({
  to,
  children,
  ...rest
}: { to: string; children: ReactNode } & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a href={to} onClick={(event) => follow(event, to)} {...rest}>
      {children}
    </a>
  );
}
