---
description: The doc: all of it the first time, then the changes since this worker last got it
header: "{{^previous}}Here is my spec, please implement it:{{/previous}}{{#previous}}I changed my spec, please apply these changes (diff, then the full spec):{{/previous}}{{#hand_edits}} I also edited code by hand, keep my changes (listed at the end).{{/hand_edits}}"
requires: [changed]
marks_sent: true
---
{{#previous}}
```diff
{{diff}}```

Full spec now:

{{/previous}}
{{doc}}
