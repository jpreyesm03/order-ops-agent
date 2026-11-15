import type { Metadata } from "next";
import { Geist, Geist_Mono, Instrument_Serif } from "next/font/google";
import { Nav } from "@/components/Nav";
import { WarmUp } from "@/components/WarmUp";
import { REPO_URL } from "@/data/results";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
const serif = Instrument_Serif({
  variable: "--font-serif-display",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});

export const metadata: Metadata = {
  title: "Order Ops Agent",
  description:
    "A customer-service agent that cancels, refunds and exchanges orders under a written policy, with the risky parts enforced in code and big refunds approved by a human.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} ${serif.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-background text-foreground">
        <WarmUp />
        <Nav />
        <main className="mx-auto w-full max-w-7xl flex-1 px-5 pb-24 sm:px-8">{children}</main>
        <footer className="border-t border-line">
          <div className="mx-auto flex max-w-7xl flex-col gap-3 px-5 py-8 text-xs text-muted sm:flex-row sm:items-center sm:px-8">
            <span className="font-serif text-base text-foreground">Order Ops</span>
            <span>Demo store data from τ²-bench (Sierra Research, MIT license). No real orders or payments.</span>
            <a href={REPO_URL} target="_blank" rel="noreferrer" className="link-grow sm:ml-auto">
              Source on GitHub ↗
            </a>
          </div>
        </footer>
      </body>
    </html>
  );
}
