Changes from 1.5:
1. Changes within the `digital_help_used` to include the `question` key within the `payload_json`. The `question` consists of the question asked for the chat's response.
 The `/telemetry/events` service operation within `coaching-platform` takes in a list of events for a particular community health worker (`chw_id`) and tenant (`tenant_id`), the request for the service operation looks like below:
 ```json
 {
	 "sdk_version": "1.4.2-android",
	 "chw_id": "", // The ID for the community health worker as per the Spice
	 "tenant_id": "", // The tenant Id for the current application
	 "events":[..] // Specific events are described below that we plan on capturing
 }
 ```

 Micro Coaching: Captures the events during the micro coaching for a community health worker. The micro coaching is accessed via the morning cards (which are generated via the `onHomeScreenShown` hook) or explicitly through the micro coaching tile shown to the community health worker on the home screen. The events of interest are described below:
2. Quiz Question Answered: When the community health worker answers a particular quiz related to a module. This can be a part of the morning refresher cards or from the learning material. Consequences for the quiz being answered:
	1. Recording progress for the CHW  against the learning material he has (like what all module he has gone through, etc.)
	2. Deriving the gap state: If the CHW answers incorrectly for a quiz, we register a gap for him against the particular module. This is used for presenting the refresher cards to him.
```json
 {
    "id": "e7", // Id of the event as attached by the SDK (should  be UUID)
    "event_schema_version": 1, // Should be 1 for the current phase
    "session_id": "sess-550e8400-e29b", // Session ID for the CHW
    "patient_visit_id": null, // Not required for the micro coaching
    "patient_track_id": null, // Not required for the micro coaching
    "patient_id_hash": null, // Not required for the micro coaching
    "village_id": null, // Not required for the micro coaching
    "upazila_id": null, // Relevant during the patient interaction
    "event_family": "coaching", // Coaching event family
    "event_type": "module_quiz_attempted", // Sub type for the event family
    "module_family_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e", // The ID for the module family to which the module belonged
    "module_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e", // The ID of the module for which the card belongs to
            card_id: null, // Not set for the quiz answered event
            quiz_id: "6375c1ba-53a7-44a2-9c4c-1c772126f32e", // The ID for the module quiz which was shown
    "module_version": 1, // The version of the module which was used
    "trigger_type": "gap", // The reason this event happened; "gap" => Telemetry Backed, "workflow"=> Upcoming Visit Trigger
    "inference_mode": null, // Not relevant for the micro coaching
    "outcome": "correct", // For the quiz_answered event we need to set the outcome as per the user input
    "validator_status": null, // Not relevant for the quiz_answered event
    "fallback_used": null, // Not relevant for the quiz_answered event
    "network_state": "online", // Network state when the event was recorded
    "payload_json": null, // Can Pass user input: {"user_input":{1, 2}}; Trigger Details etc.
    "event_date": "2026-04-28", // Date of the event
    "timestamp_utc": 1713969360, // UTC timestamp for the event
    "timestamp_local": 1713990960, // Local timestamp for the event
    "quiz_score_pct": null // Current quize score percentage
}
```
2. Module Requested: When the community health worker submits the request for an additional module to be included within his/her quota on the application, the below event should get registered. We require either the top level `module_id` or the `payload_json`should have `requested_module_name` in it. The reason could be included inside the `reason` field within the `payload_json`.
```json
{
    "id": "e7", // Id of the event as attached by the SDK (should  be UUID)
    "event_schema_version": 1, // Should be 1 for the current phase
    "session_id": "sess-550e8400-e29b", // Session ID for the CHW
    "patient_visit_id": null, // Not required for the micro coaching
    "patient_track_id": null, // Not required for the micro coaching
    "patient_id_hash": null, // Not required for the micro coaching
    "village_id": null, // Not required for the micro coaching
    "upazila_id": null, // Relevant during the patient interaction
    "event_family": "coaching", // Coaching event family
    "event_type": "module_requested", // Sub type for the event family
    "module_family_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e", // Optional
    "module_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e", // The ID of the module for which the user wants the request to be
            card_id: null, // Not set
            quiz_id: null, // Not set
    "module_version": null, // Not set
    "trigger_type": null, // Not set
    "inference_mode": null, // Not relevant for the micro coaching
    "outcome": null, // Not required
    "validator_status": null, // Not relevant
    "fallback_used": null, // Not relevant
    "network_state": "online", // Network state when the event was recorded
    "payload_json": null, // {"requested_module_name":"ANC", "reason":"No idea what ANC is"} specify the reason and free hand module name here
    "event_date": "2026-04-28", // Date of the event
    "timestamp_utc": 1713969360, // UTC timestamp for the event
    "timestamp_local": 1713990960, // Local timestamp for the event
    "quiz_score_pct": null // Current quize score percentage
}
```

Apart from the above, we also collect `digital` events corresponding to CHW's interaction with the chat bot. These are mainly used for analytics purposes (How useful the chat functionality is to the user? How frequently the feature is being used?, etc.)
1. Digital Help Used: Events recorded during the interaction of community help worker with the chat bot on the SDK. These events are recorded whenever the response generation takes place for the CHW's query.
```json
{
    "id": "e7", // Id of the event as attached by the SDK (should  be UUID)
    "event_schema_version": 1, // Should be 1 for the current phase
    "session_id": "sess-550e8400-e29b", // Session ID for the CHW
    "patient_visit_id": null, // Not required for the it help
    "patient_track_id": null, // Not required for the it help
    "patient_id_hash": null, // Not required for the it help
    "village_id": null, // Not required
    "upazila_id": null, // Not required
    "event_family": "digital", // Coaching event family
    "event_type": "digital_help_used", // Sub type for the event family
    "module_family_id": null, // not required
    "module_id": 'value', // required from the rag-query's response
            card_id: null, // Not required
            quiz_id: null, // Not required
    "trigger_type": "workflow_event", // Reason for this event
    "inference_mode": "online", // Depends how the response was computed
    "outcome": null, // Not required
    "validator_status": "pass", // Validator's status on the output
    "fallback_used": false, // If the fallback was used for responding
    "network_state": "online", // Network state when the event was recorded
    "payload_json": null, // No extra information
    "event_date": "2026-04-28", // Date of the event
    "timestamp_utc": 1713969360, // UTC timestamp for the event
    "timestamp_local": 1713990960, // Local timestamp for the event
    "module_version": null, // Not required
    "quiz_score_pct": null, // Not required
    "payload_json": {
			    "question": "What was the question by the user during the interaction?",// User's question on the chat
			    "response": {
				    "answer": "Answer to the above question.",
				    "retrieved_modules": [],
				    "source_documents": [],
				    "model": "fable-5",
				    "cited_module_ids": [],
				    "suggested_questions": []
			    },// The response we have gotten either from the online RAG or the offline retrieval, we assume same shape for both the response
            }
}
```
2. Chat Feedback Positive: The event indicates that the response generated for the user's query was positive/helpful.
```json
{
    "id": "e7", // Id of the event as attached by the SDK (should  be UUID)
    "event_schema_version": 1, // Should be 1 for the current phase
    "session_id": "sess-550e8400-e29b", // Session ID for the CHW
    "patient_visit_id": null, // Not required for the it help
    "patient_track_id": null, // Not required for the it help
    "patient_id_hash": null, // Not required for the it help
    "village_id": null, // Not required
    "upazila_id": null, // Not required
    "event_family": "digital", // Coaching event family
    "event_type": "chat_feedback_positive", // Sub type for the event family
    "module_family_id": null, // not required
    "module_id": 'value', // required from the rag-query's response
            card_id: null, // Not required
            quiz_id: null, // Not required
    "trigger_type": "workflow_event", // Reason for this event
    "inference_mode": "online", // Depends how the response was computed
    "outcome": null, // Not required
    "validator_status": "pass", // Validator's status on the output
    "fallback_used": false, // If the fallback was used for responding
    "network_state": "online", // Network state when the event was recorded
    "payload_json": null, // No extra information
    "event_date": "2026-04-28", // Date of the event
    "timestamp_utc": 1713969360, // UTC timestamp for the event
    "timestamp_local": 1713990960, // Local timestamp for the event
    "module_version": null, // Not required
    "quiz_score_pct": null, // Not required
    "payload_json": {
			    "question": "What was the question by the user during the interaction?",// User's question on the chat
			    "response": {
				    "answer": "Answer to the above question.",
				    "retrieved_modules": [],
				    "source_documents": [],
				    "model": "fable-5",
				    "cited_module_ids": [],
				    "suggested_questions": []
			    },// The response we have gotten either from the online RAG or the offline retrieval, we assume same shape for both the response
			    "feedback": "The answer satisfied my expectation."
            }
}
```
3. Chat Feedback Negative: The event indicates that the response was not correct to the user's expectation and we should strive to do better.
```json
{
    "id": "e7", // Id of the event as attached by the SDK (should  be UUID)
    "event_schema_version": 1, // Should be 1 for the current phase
    "session_id": "sess-550e8400-e29b", // Session ID for the CHW
    "patient_visit_id": null, // Not required for the it help
    "patient_track_id": null, // Not required for the it help
    "patient_id_hash": null, // Not required for the it help
    "village_id": null, // Not required
    "upazila_id": null, // Not required
    "event_family": "digital", // Coaching event family
    "event_type": "chat_feedback_negative", // Sub type for the event family
    "module_family_id": null, // not required
    "module_id": 'value', // required from the rag-query's response
            card_id: null, // Not required
            quiz_id: null, // Not required
    "trigger_type": "workflow_event", // Reason for this event
    "inference_mode": "online", // Depends how the response was computed
    "outcome": null, // Not required
    "validator_status": "pass", // Validator's status on the output
    "fallback_used": false, // If the fallback was used for responding
    "network_state": "online", // Network state when the event was recorded
    "payload_json": null, // No extra information
    "event_date": "2026-04-28", // Date of the event
    "timestamp_utc": 1713969360, // UTC timestamp for the event
    "timestamp_local": 1713990960, // Local timestamp for the event
    "module_version": null, // Not required
    "quiz_score_pct": null, // Not required
    "payload_json": {
			    "question": "What was the question by the user during the interaction?",// User's question on the chat
			    "response": {
				    "answer": "Answer to the above question.",
				    "retrieved_modules": [],
				    "source_documents": [],
				    "model": "fable-5",
				    "cited_module_ids": [],
				    "suggested_questions": []
			    },// The response we have gotten either from the online RAG or the offline retrieval, we assume same shape for both the response
			    "feedback": "The answer was poor and did not satisfy what i wanted to convey."
            }
}
```
