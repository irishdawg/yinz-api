import { QRCodeSVG } from "qrcode.react";

export default async function GamePage({ searchParams }: { searchParams: Promise<{ code?: string }> }) {
  const { code } = await searchParams;

  if (!code) {
    return (
      <div className="flex flex-1 items-center justify-center bg-zinc-50">
        <p className="text-zinc-600">Missing join code.</p>
      </div>
    );
  }

  const joinUrl = `${process.env.NEXT_PUBLIC_SITE_URL ?? ""}/join/${code}`;

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-zinc-50 px-6">
      <h1 className="text-2xl font-semibold text-zinc-900">Game created</h1>
      <p className="text-sm text-zinc-600">Share this code or QR to invite players:</p>
      <p className="font-mono text-4xl font-bold tracking-widest text-zinc-900">{code}</p>
      <div className="rounded bg-white p-4 shadow">
        <QRCodeSVG value={joinUrl} size={200} />
      </div>
      <p className="max-w-xs break-all text-center text-xs text-zinc-500">{joinUrl}</p>
    </div>
  );
}
