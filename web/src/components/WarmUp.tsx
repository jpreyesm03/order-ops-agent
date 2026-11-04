"use client";

import { useEffect } from "react";
import { API_URL } from "@/lib/api";

// The API runs on AWS Lambda: the first request after a quiet period starts a fresh instance (~5 s).
// Ping it as soon as any page opens, so it is warm by the time the visitor sends a message.
export function WarmUp() {
  useEffect(() => {
    fetch(`${API_URL}/healthz`, { cache: "no-store" }).catch(() => {});
  }, []);
  return null;
}
