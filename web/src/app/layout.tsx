import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Karting Telemetry",
  description: "Kart racing telemetry analysis",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link
          href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-screen bg-canvas font-sans text-ink antialiased">{children}</body>
    </html>
  );
}
