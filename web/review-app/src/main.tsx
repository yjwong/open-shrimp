import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { init, disableVerticalSwipes } from "@telegram-apps/sdk-react";
import App from "./App";
import "./styles/global.css";

// Initialize Telegram Mini App SDK
init();
disableVerticalSwipes();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
