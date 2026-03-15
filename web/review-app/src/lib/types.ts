export interface HunkLine {
  type: "context" | "add" | "delete";
  old_no: number | null;
  new_no: number | null;
  content: string;
}

export interface Hunk {
  id: string;
  file_path: string;
  language: string;
  is_new_file: boolean;
  is_deleted_file: boolean;
  is_binary: boolean;
  hunk_header: string;
  lines: HunkLine[];
  staged: boolean;
}

export interface FileSummary {
  path: string;
  first_hunk_index: number;
  hunk_count: number;
  staged_count: number;
}

export interface HunkResult {
  context: string;
  directory: string;
  total_hunks: number;
  offset: number;
  hunks: Hunk[];
  files: FileSummary[];
}
