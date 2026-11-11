import type { NextConfig } from "next";

// The rewrite destination is written into the routes manifest at build time, so
// API_ORIGIN must be set when `next build` runs; setting it on the running server does nothing.
const API_ORIGIN = process.env.API_ORIGIN ?? "http://localhost:8000";

const config: NextConfig = {
  output: "standalone",
  experimental: {
    // The rewrite proxy truncates bodies past 10 MB by default; the API accepts 50 MiB uploads.
    proxyClientMaxBodySize: "60mb",
    // A real-model answer can outlast the proxy's default timeout.
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/:path*` }];
  },
};

export default config;
