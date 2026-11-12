on run argv
    set recipientAddress to item 1 of argv
    set senderAddress to item 2 of argv
    set subjectText to item 3 of argv
    set bodyText to read POSIX file (item 4 of argv) as «class utf8»
    with timeout of 60 seconds
        tell application "Mail"
            set outgoingMessage to make new outgoing message with properties {subject:subjectText, content:bodyText, sender:senderAddress, visible:false}
            -- Styled text makes Mail preserve the fixed-width table in rich-text email.
            set font of content of outgoingMessage to "Courier"
            set size of content of outgoingMessage to 11
            set font of paragraph 1 of content of outgoingMessage to "Courier-Bold"
            set size of paragraph 1 of content of outgoingMessage to 13
            tell outgoingMessage
                make new to recipient at end of to recipients with properties {address:recipientAddress}
            end tell
            if (send outgoingMessage) is false then error "Apple Mail did not accept the message"
        end tell
    end timeout
    return "Submitted to Apple Mail"
end run
