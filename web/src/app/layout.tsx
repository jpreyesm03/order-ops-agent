import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { WarmUp } from "@/components/WarmUp";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Order Ops Agent",
  description:
    "A customer-service agent that cancels, refunds and exchanges orders under a written policy, with the risky parts enforced in code and big refunds approved by a human.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <WarmUp />
        <header className="border-b border-line">
          <nav className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-3 text-sm">
            <Link href="/" className="font-semibold tracking-tight">
              Order Ops Agent
            </Link>
            <Link href="/" className="text-muted hover:text-foreground">
              Customer chat
            </Link>
            <Link href="/console" className="text-muted hover:text-foreground">
              Supervisor console
            </Link>
            <Link href="/how-it-works" className="text-muted hover:text-foreground">
              How it works
            </Link>
          </nav>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">{children}</main>
        <footer className="border-t border-line px-4 py-4 text-center text-xs text-muted">
          Demo store data from τ²-bench (Sierra Research, MIT license). No real orders or payments.
        </footer>
      </body>
    </html>
  );
}
