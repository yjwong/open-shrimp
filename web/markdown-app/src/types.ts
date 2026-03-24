export interface Comment {
  id: string;
  blockIndex: number;
  blockText: string;
  comment: string;
  createdAt: number;
}

export interface DocumentData {
  path: string;
  filename: string;
  content: string;
}
