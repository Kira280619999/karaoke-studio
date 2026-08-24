import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';
import { karaokeFontFaceCss } from './lib/karaoke-fonts';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_ORIGIN ?? 'http://127.0.0.1:3000'),
  title: 'Karaoke Studio — Karaoke kiểm chứng theo frame',
  description: 'Biến MP4 và timeline LRC/SRT/VTT thành video Karaoke local được tách giọng, căn lời và kiểm chứng theo từng frame.',
  openGraph: {
    title: 'Karaoke Studio',
    description: 'MP4 + LRC/SRT/VTT → Karaoke 1080p120 được kiểm chứng theo frame, xử lý hoàn toàn trên máy.',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: 'Karaoke Studio — Frame-Verified Karaoke' }],
    locale: 'vi_VN',
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Karaoke Studio',
    description: 'MP4 + timeline đa định dạng → Karaoke kiểm chứng theo frame.',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const apiBase = process.env.NEXT_PUBLIC_KARAOKE_API ?? 'http://127.0.0.1:8000';
  return (
    <html lang="vi">
      <head>
        <style id="karaoke-font-faces">{karaokeFontFaceCss(apiBase)}</style>
      </head>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
