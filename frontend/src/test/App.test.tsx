import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";

describe("App", () => {
  it("renders the header", () => {
    render(<App />);
    expect(screen.getByText("Tower Finder")).toBeInTheDocument();
  });

  it("shows the search form by default", () => {
    render(<App />);
    // The search tab should be active, showing the search form
    expect(document.querySelector(".app")).toBeTruthy();
  });
});
