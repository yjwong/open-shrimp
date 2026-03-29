import { defineConfig } from "vite";

export default defineConfig({
  base: "/vnc/",
  build: {
    // noVNC uses top-level await for H.264 detection.
    target: "esnext",
  },
});
