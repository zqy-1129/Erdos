export interface ArtifactView { id: string; name: string; stage: string; kind: string; sha256: string; size: number }
export interface ArtifactPreview { kind: "text" | "image" | "binary"; text?: string; imageUrl?: string; name: string }
