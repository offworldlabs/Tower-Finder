import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import LoginPage from "../pages/LoginPage";

// Mock the auth context to return no user (unauthenticated)
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, logout: vi.fn() }),
}));

describe("LoginPage", () => {
  it("renders the login card", () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    );
    expect(screen.getByText("Retina")).toBeInTheDocument();
    expect(screen.getByText("Passive Radar Network Dashboard")).toBeInTheDocument();
  });

  it("shows a Google login link", () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    );
    const links = screen.getAllByRole("link");
    const googleLink = links.find((l) =>
      l.getAttribute("href")?.includes("/api/auth/login/google")
    );
    expect(googleLink).toBeTruthy();
  });
});
