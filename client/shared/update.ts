export interface UpdateView {
  status: "disabled" | "idle" | "checking" | "current" | "available" | "downloading" | "downloaded" | "error";
  currentVersion: string;
  version: string | null;
  progress: number;
  message: string;
}
