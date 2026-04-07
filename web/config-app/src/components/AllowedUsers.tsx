import { useCallback, useState } from "react";

interface AllowedUsersProps {
  users: number[];
  onChange: (users: number[]) => void;
}

export default function AllowedUsers({ users, onChange }: AllowedUsersProps) {
  const [input, setInput] = useState("");

  const addUser = useCallback(() => {
    const id = parseInt(input.trim());
    if (!isNaN(id) && !users.includes(id)) {
      onChange([...users, id]);
      setInput("");
    }
  }, [input, users, onChange]);

  const removeUser = useCallback(
    (idx: number) => {
      if (users.length <= 1) return; // Prevent empty list.
      onChange(users.filter((_, i) => i !== idx));
    },
    [users, onChange],
  );

  return (
    <div className="users-section">
      <div className="user-chips">
        {users.map((id, i) => (
          <span key={id} className="user-chip">
            {id}
            {users.length > 1 && (
              <button
                type="button"
                className="user-chip-remove"
                onClick={() => removeUser(i)}
              >
                x
              </button>
            )}
          </span>
        ))}
      </div>
      <div className="add-user-row">
        <input
          className="form-input"
          type="number"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addUser();
            }
          }}
          placeholder="Telegram user ID"
        />
        <button
          type="button"
          className="btn btn-secondary btn-small"
          onClick={addUser}
          disabled={!input.trim() || isNaN(parseInt(input))}
        >
          Add
        </button>
      </div>
    </div>
  );
}
