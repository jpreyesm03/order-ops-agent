"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { REPO_URL } from "@/data/results";

const LINKS = [
  { href: "/", label: "Try it", short: "Try it", n: "01" },
  { href: "/console", label: "Supervisor console", short: "Console", n: "02" },
  { href: "/how-it-works", label: "How it works", short: "How", n: "03" },
];

export function Nav() {
  const path = usePathname();
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-background/85 backdrop-blur">
      <nav className="mx-auto flex max-w-7xl items-center gap-x-6 px-5 py-4 sm:gap-x-8 sm:px-8">
        <Link href="/" className="flex shrink-0 items-baseline gap-2 whitespace-nowrap">
          <span className="font-serif text-xl leading-none">Order Ops</span>
          <span className="eyebrow hidden sm:inline">Agent</span>
        </Link>
        <div className="ml-auto flex items-center gap-x-4 text-sm sm:gap-x-6">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              aria-current={path === l.href ? "page" : undefined}
              className="link-grow flex shrink-0 items-baseline gap-1.5 text-muted transition-colors hover:text-foreground aria-[current=page]:text-foreground"
            >
              <span className="hidden font-mono text-[10px] text-faint sm:inline">{l.n}</span>
              <span className="sm:hidden">{l.short}</span>
              <span className="hidden sm:inline">{l.label}</span>
            </Link>
          ))}
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="hidden shrink-0 rounded-full border border-line px-3 py-1 text-xs transition-colors hover:border-foreground sm:inline"
          >
            GitHub ↗
          </a>
        </div>
      </nav>
    </header>
  );
}
