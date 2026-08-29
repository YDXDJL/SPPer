import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "冠影守望者 · 泳池安全监控",
  description: "泳池智能安全防护系统电脑端",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
