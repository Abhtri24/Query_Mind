import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "QueryMind — Talk to your database",
  description: "Connect any database. Ask questions in plain English. Self-healing SQL.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
