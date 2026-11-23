const messageLine = /^(\s*\S+\s*(?:--?>>?|--?x|--?\)|<<--?>>)\s*\S+\s*:)(.*)$/;
const statement = /^(?:participant|actor|activate|deactivate|create|destroy|note|loop|alt|else|opt|par|and|critical|option|break|rect|end|autonumber|title|accTitle|accDescr)\b/i;

export function prepareMermaidSource(source) {
  if (!/^\s*sequenceDiagram\b/m.test(source)) return source;
  return source.split(/\r?\n/).map((line) => {
    const match = line.match(messageLine);
    if (!match) return line;
    const message = match[2].replace(/;/g, (semicolon, offset, text) => {
      const before = text.slice(0, offset);
      const after = text.slice(offset + 1).trim();
      // Preserve valid statement separators and existing encoded punctuation.
      if (!after || /[#&](?:#?\d+|[a-zA-Z]+)$/.test(before) || messageLine.test(after) || statement.test(after)) {
        return semicolon;
      }
      return "#59;";
    });
    return match[1] + message;
  }).join("\n");
}
